"""Carte satellite interactive (Tkinter) pour choisir une zone en dessinant un polygone.

Fonds de carte : Esri World Imagery (monde), orthophotographie IGN (France), OpenStreetMap (plan).
Les tuiles sont téléchargées à la demande dans des threads, puis affichées sur un Canvas.
"""
import io
import math
import queue
import threading
import time
import tkinter as tk
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

import theme
import topo

TILE = topo.TILE
MIN_Z, MAX_Z = 3, 19
LAYERS = topo.TILE_LAYERS
world_xy = topo.world_xy
POLY_RGB = (255, 170, 40)  # même orange que le recadrage de l'aperçu 3D
HIT = 10  # rayon de saisie d'un sommet (px)


def world_latlon(x, y, z):
    n = TILE * 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))), x / n * 360 - 180


class MapDialog(tk.Toplevel):
    """Fenêtre de choix de zone. `on_ok(polygone [(lat, lon), …], vue (lat, lon, zoom), libellé)` est appelé
    quand l'utilisateur valide un polygone fermé."""

    def __init__(self, master, on_ok, view=(46.6, 2.5, 6), poly=None, layer=None, label=""):
        super().__init__(master, bg=theme.BG)
        self.title("Choisir la zone — carte")
        self.geometry("1100x760+60+40")
        self.minsize(720, 520)
        self.transient(master)
        self.on_ok = on_ok
        self.label = label
        self.layer = tk.StringVar(value=layer if layer in LAYERS else next(iter(LAYERS)))
        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.pending = set()
        self.raw = OrderedDict()  # (couche, z, x, y) -> image PIL | None (échec)
        self.photos = OrderedDict()  # idem -> PhotoImage (créée dans le thread de l'interface)
        self.items = {}  # clé de tuile -> id d'objet du canvas
        self.lat, self.lon, self.z = view
        self.cx = self.cy = 0.0
        self.pts = [tuple(p) for p in (poly or [])]
        self.closed = len(self.pts) >= 3
        self.drag = None
        self.dbl = False
        self.wheel_acc, self.wheel_t = 0.0, 0.0
        self._fill = None  # PhotoImage de remplissage du polygone
        self._alive = True
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<Return>", lambda e: self.close_polygon())
        self.bind("<BackSpace>", lambda e: self.undo())
        self.bind("<Delete>", lambda e: self.undo())
        self.bind("<plus>", lambda e: self.zoom_step(1))
        self.bind("<equal>", lambda e: self.zoom_step(1))
        self.bind("<minus>", lambda e: self.zoom_step(-1))
        self.update_idletasks()
        if self.closed:
            self.fit(self.pts)
        else:
            self.cx, self.cy = world_xy(self.lat, self.lon, self.z)
        self.after(50, self._poll)
        self.after(80, self.redraw)

    # --- interface ---------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 6))
        self.search = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: (self.goto_place(), "break")[1])
        ttk.Button(top, text="Aller", style="Tool.TButton", command=self.goto_place).pack(side="left", padx=(6, 12))
        c = ttk.Combobox(top, textvariable=self.layer, values=list(LAYERS), state="readonly", width=24)
        c.pack(side="left")
        c.bind("<<ComboboxSelected>>", lambda ev: self._layer_changed())

        mid = tk.Frame(self, bg=theme.EDGE, bd=0)
        mid.pack(fill="both", expand=True, padx=10)
        self.cv = tk.Canvas(mid, bg=theme.PANEL, highlightthickness=0, cursor="crosshair")
        self.cv.pack(fill="both", expand=True, padx=1, pady=1)
        self.cv.bind("<Configure>", lambda e: self.redraw())
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Double-Button-1>", self._double)
        for b in ("<Button-2>", "<Button-3>"):
            self.cv.bind(b, lambda e: self.undo())
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", lambda e: self.zoom_step(1, e.x, e.y))  # Linux
        self.cv.bind("<Button-5>", lambda e: self.zoom_step(-1, e.x, e.y))

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(6, 10))
        ttk.Button(bar, text="−", width=3, style="Tool.TButton", command=lambda: self.zoom_step(-1)).pack(side="left")
        ttk.Button(bar, text="+", width=3, style="Tool.TButton", command=lambda: self.zoom_step(1)).pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="Fermer le polygone", style="Tool.TButton", command=self.close_polygon).pack(side="left")
        ttk.Button(bar, text="Annuler le dernier point", style="Tool.TButton", command=self.undo).pack(side="left", padx=6)
        ttk.Button(bar, text="Tout effacer", style="Tool.TButton", command=self.clear).pack(side="left")
        self.btn_ok = ttk.Button(bar, text="Télécharger le relief", style="Accent.TButton", command=self.validate)
        self.btn_ok.pack(side="right")
        self.info = ttk.Label(bar, text="", style="Muted.TLabel")
        self.info.pack(side="right", padx=14)
        self.hint = ttk.Label(self, style="Muted.TLabel", anchor="w", wraplength=1060, text=(
            "Clic : ajouter un point · double-clic ou Entrée : fermer le polygone · glisser : déplacer la carte "
            "(ou un point) · molette : zoom · clic droit ou ⌫ : retirer le dernier point."))
        self.hint.pack(fill="x", padx=10, pady=(0, 8))
        self._update_info()

    # --- tuiles -----------------------------------------------------------------
    def _fetch(self, key):
        layer, z, x, y = key
        try:
            req = urllib.request.Request(LAYERS[layer][0].format(z=z, x=x, y=y), headers={"User-Agent": topo.UA})
            with urllib.request.urlopen(req, timeout=20, context=topo._ctx()) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
        except Exception:  # noqa: BLE001
            img = None
        self.q.put((key, img))

    def _poll(self):
        if not self._alive:
            return
        got = False
        try:
            while True:
                key, img = self.q.get_nowait()
                self.pending.discard(key)
                self.raw[key] = img
                got = True
        except queue.Empty:
            pass
        if got:
            while len(self.raw) > 600:
                self.raw.popitem(last=False)
            self.redraw()
        self.after(60, self._poll)

    def _photo(self, key):
        ph = self.photos.get(key)
        if ph is None and self.raw.get(key) is not None:
            ph = self.photos[key] = ImageTk.PhotoImage(self.raw[key])
            while len(self.photos) > 300:
                self.photos.popitem(last=False)
        return ph

    # --- affichage ----------------------------------------------------------------
    def _canvas_size(self):
        return max(self.cv.winfo_width(), 50), max(self.cv.winfo_height(), 50)

    def to_canvas(self, lat, lon):
        w, h = self._canvas_size()
        x, y = world_xy(lat, lon, self.z)
        return x - self.cx + w / 2, y - self.cy + h / 2

    def from_canvas(self, px, py):
        w, h = self._canvas_size()
        return world_latlon(px + self.cx - w / 2, py + self.cy - h / 2, self.z)

    def redraw(self):
        if not self._alive:
            return
        w, h = self._canvas_size()
        layer, n = self.layer.get(), 2 ** self.z
        x0, y0 = self.cx - w / 2, self.cy - h / 2
        want = set()
        for ty in range(max(0, int(y0 // TILE)), min(n - 1, int((y0 + h) // TILE)) + 1):
            for tx in range(int(x0 // TILE), int((x0 + w) // TILE) + 1):
                key = (layer, self.z, tx % n, ty)
                px, py = tx * TILE - x0, ty * TILE - y0
                want.add((key, tx))
                item = self.items.get((key, tx))
                ph = self._photo(key)
                if ph is not None:
                    if item is None:
                        self.items[(key, tx)] = self.cv.create_image(px, py, image=ph, anchor="nw", tags="tile")
                    else:
                        self.cv.coords(item, px, py)
                elif key not in self.raw and key not in self.pending:
                    self.pending.add(key)
                    self.pool.submit(self._fetch, key)
        for k in [k for k in self.items if k not in want]:
            self.cv.delete(self.items.pop(k))
        self.cv.tag_lower("tile")
        self._draw_overlay(w, h)

    def _draw_overlay(self, w, h):
        self.cv.delete("ov")
        pts = [self.to_canvas(la, lo) for la, lo in self.pts]
        if len(pts) >= 3 and self.closed:  # remplissage translucide (PIL : Tk n'a pas de transparence)
            im = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
            ImageDraw.Draw(im).polygon(pts, fill=POLY_RGB + (70,))
            self._fill = ImageTk.PhotoImage(im)
            self.cv.create_image(0, 0, image=self._fill, anchor="nw", tags="ov")
        col = "#%02x%02x%02x" % POLY_RGB
        if len(pts) >= 2:
            flat = [c for p in pts for c in p] + (list(pts[0]) if self.closed else [])
            self.cv.create_line(*flat, fill=col, width=2, tags="ov")
        for i, (x, y) in enumerate(pts):
            r = 6 if i == 0 and not self.closed else 4
            self.cv.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff" if i == 0 else col, outline="#000000", tags="ov")
        txt = LAYERS[self.layer.get()][1]
        t = self.cv.create_text(w - 8, h - 8, text=txt, anchor="se", fill="#ffffff", font=(theme.family(self), 10), tags="ov")
        bb = self.cv.bbox(t)  # fond sombre derrière l'attribution pour qu'elle reste lisible sur l'imagerie
        self.cv.tag_lower(self.cv.create_rectangle(bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2, fill="#000000",
                                                   outline="", tags="ov"), t)
        self.cv.tag_raise("ov")

    # --- navigation ---------------------------------------------------------------
    def zoom_step(self, d, px=None, py=None):
        nz = max(MIN_Z, min(MAX_Z, self.z + d))
        if nz == self.z:
            return
        w, h = self._canvas_size()
        px, py = (w / 2, h / 2) if px is None else (px, py)
        la, lo = self.from_canvas(px, py)  # le point sous le curseur reste sous le curseur
        self.z = nz
        x, y = world_xy(la, lo, nz)
        self.cx, self.cy = x - px + w / 2, y - py + h / 2
        self.redraw()

    def _wheel(self, e):
        now = time.time()
        if now - self.wheel_t > 0.3:
            self.wheel_acc = 0.0
        self.wheel_t = now
        self.wheel_acc += e.delta
        if abs(self.wheel_acc) >= 3:  # regroupe les micro-événements du trackpad en crans
            d = 1 if self.wheel_acc > 0 else -1
            self.wheel_acc = 0.0
            self.zoom_step(d, e.x, e.y)

    def fit(self, pts):
        """Cadre la vue sur une liste de points (lat, lon)."""
        w, h = self._canvas_size()
        for z in range(MAX_Z, MIN_Z - 1, -1):
            xs, ys = zip(*(world_xy(la, lo, z) for la, lo in pts))
            if max(xs) - min(xs) < w * 0.8 and max(ys) - min(ys) < h * 0.8:
                break
        self.z = z
        self.cx, self.cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def goto_place(self):
        q = self.search.get().strip()
        if not q:
            return
        self.info.config(text="Recherche…")

        def work():
            try:
                la, lo, lab = topo.geocode(q)
                self.after(0, lambda: self._goto(la, lo, lab))
            except Exception as ex:  # noqa: BLE001
                self.after(0, lambda: self.info.config(text=str(ex)))

        threading.Thread(target=work, daemon=True).start()

    def _goto(self, la, lo, lab):
        if not self._alive:
            return
        self.label = lab.split(",")[0]
        self.z = max(self.z, 16)
        self.cx, self.cy = world_xy(la, lo, self.z)
        self._update_info()
        self.redraw()

    def _layer_changed(self):
        for k in list(self.items):
            self.cv.delete(self.items.pop(k))
        self.redraw()

    # --- polygone -----------------------------------------------------------------
    def _vertex_at(self, x, y):
        for i, (la, lo) in enumerate(self.pts):
            px, py = self.to_canvas(la, lo)
            if (px - x) ** 2 + (py - y) ** 2 <= HIT ** 2:
                return i
        return None

    def _press(self, e):
        self.dbl = False
        self.drag = {"x": e.x, "y": e.y, "cx": self.cx, "cy": self.cy, "moved": False, "v": self._vertex_at(e.x, e.y)}

    def _motion(self, e):
        d = self.drag
        if d is None:
            return
        if not d["moved"] and abs(e.x - d["x"]) + abs(e.y - d["y"]) < 5:
            return
        d["moved"] = True
        if d["v"] is not None:  # déplacement d'un sommet
            self.pts[d["v"]] = self.from_canvas(e.x, e.y)
            self._update_info()
        else:
            self.cx, self.cy = d["cx"] - (e.x - d["x"]), d["cy"] - (e.y - d["y"])
        self.redraw()

    def _release(self, e):
        d, self.drag = self.drag, None
        if d is None or d["moved"]:
            return
        if self.dbl:
            self.close_polygon()
            return
        if self.closed:
            return
        if d["v"] == 0 and len(self.pts) >= 3:  # clic sur le premier point : on ferme
            self.close_polygon()
            return
        self.pts.append(self.from_canvas(e.x, e.y))
        self._update_info()
        self.redraw()

    def _double(self, e):
        self.dbl = True

    def close_polygon(self):
        if len(self.pts) >= 3 and not self.closed:
            self.closed = True
            self._update_info()
            self.redraw()

    def undo(self):
        if self.closed:
            self.closed = False
        elif self.pts:
            self.pts.pop()
        self._update_info()
        self.redraw()

    def clear(self):
        self.pts, self.closed = [], False
        self._update_info()
        self.redraw()

    def _update_info(self):
        ok = self.closed and len(self.pts) >= 3
        if ok:
            sx, sy, area = topo.poly_extent_m(self.pts)
            txt = "%d points · emprise %.0f × %.0f m · %s" % (
                len(self.pts), sx, sy, ("%.2f ha" % (area / 1e4)) if area < 1e7 else ("%.1f km²" % (area / 1e6)))
            if max(sx, sy) > 20000:
                txt, ok = "Zone trop grande (max 20 km de côté)", False
            elif max(sx, sy) < 20:
                txt, ok = "Zone trop petite (min 20 m)", False
        else:
            txt = ("Place au moins 3 points, puis ferme le polygone" if len(self.pts) < 3
                   else "Ferme le polygone (double-clic ou Entrée)")
        self.info.config(text=txt)
        self.btn_ok.state(["!disabled"] if ok else ["disabled"])

    def validate(self):
        if not (self.closed and len(self.pts) >= 3):
            return
        la, lo = world_latlon(self.cx, self.cy, self.z)
        poly, view, label = list(self.pts), (la, lo, self.z), self.label or "Zone dessinée"
        self.close()
        self.on_ok(poly, view, label, self.layer.get())

    def close(self):
        self._alive = False
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()
