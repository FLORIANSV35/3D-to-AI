"""Aperçu 3D (nuage de points échantillonné sur la surface), rendu numpy -> PhotoImage (PPM).

Optimisations : tri de profondeur 16 bits (tri radix), couleurs précalculées, pixels empaquetés en
uint32 (une écriture par point), niveau de détail réduit pendant les gestes puis image nette au repos,
et fusion des événements souris (un seul rendu par image).
"""
import tkinter as tk

import numpy as np

import obj2dae
import theme

LOD_POINTS = 70_000  # nombre de points affichés pendant un geste (rotation, zoom, déplacement)
SETTLE_MS = 160  # délai sans geste avant le rendu complet


def _pk(rgb):
    """Couleur (r, g, b) -> pixel uint32 (octets R, G, B, A en mémoire little-endian)."""
    r, g, b = rgb
    return np.uint32(r | (g << 8) | (b << 16) | (255 << 24))


class Preview(tk.Label):
    def __init__(self, master, on_moved=None):
        super().__init__(master, bg=theme.VIEWPORT, cursor="fleur", bd=0, highlightthickness=0)
        self.on_moved = on_moved  # fin de glissement du repère : delta (m, repère Z vertical)
        self.unit_m = 1.0  # mètres par unité normalisée
        self._grab = None
        self.pick_cb = None  # si défini : le prochain clic choisit un point du modèle
        self.crop = None
        self.outside = None
        self.zup = None
        self.c = np.zeros(3)
        self.pan = [0.0, 0.0]  # décalage de la vue (unités normalisées)
        self.poly = None  # polygone de recadrage terminé : points (X, Y) en repère Z vertical
        self.poly_mode = False
        self.draft = []  # polygone en cours de tracé
        self.on_poly_done = None
        self.on_poly_status = None
        self.pts = None  # (N,3) float64 normalisés, repère d'affichage (Y vertical)
        self.pts32 = None
        self.base = None  # (N,3) float32 : couleur × éclairage, sans l'effet de profondeur
        self.origin = None
        self.show_axes = True
        self.yaw, self.pitch, self.zoom = 0.6, 0.45, 1.0
        self._last = None
        self._pan_last = None
        self._img = None
        self._interactive = False
        self._sched = None
        self._settle = None
        self.bind("<Configure>", lambda e: self.request_render())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self.bind("<MouseWheel>", lambda e: self._zoom(1.1 if e.delta > 0 else 0.9, e.x, e.y))
        # clic droit (Button-2 sous macOS, Button-3 ailleurs) ou bouton du milieu + glisser : déplacer la vue
        for b in (2, 3):
            self.bind(f"<ButtonPress-{b}>", self._pan_press)
            self.bind(f"<B{b}-Motion>", self._pan_drag)
            self.bind(f"<ButtonRelease-{b}>", self._pan_release)
        self.bind("<Double-Button-1>", lambda e: None if self.poly_mode else self.reset())

    # ------------------------------------------------------------------ données
    def set_points(self, zup, normals=None, colors=None, origin=(0.0, 0.0, 0.0), keep_view=False,
                   crop=None, poly=None, max_points=250_000):
        """zup : (N,3) en repère Z vertical, déjà placés. origin : où se trouve (0,0,0) de la sortie."""
        if len(zup) > max_points:
            keep = np.random.default_rng(0).choice(len(zup), max_points, replace=False)
            zup = zup[keep]
            normals = normals[keep] if normals is not None else None
            colors = colors[keep] if colors is not None else None
        self.zup = zup
        self.crop = crop  # (lo, hi) en repère Z vertical, ou None
        self.poly = poly
        inside = None if crop is None else np.all((zup >= crop[0]) & (zup <= crop[1]), axis=1)
        if poly is not None and len(poly) >= 3 and inside is not None:
            inside &= obj2dae.points_in_polygon(zup[:, :2], poly)
        self.outside = None if inside is None else ~inside

        c = (zup.max(axis=0) + zup.min(axis=0)) / 2
        disp = lambda q: np.stack([q[..., 0], q[..., 2], -q[..., 1]], axis=-1)  # affichage Y vertical
        p = disp(zup - c)
        k = max(np.abs(p).max(), 1e-12)
        self.pts = p / k
        self.pts32 = np.ascontiguousarray(self.pts, dtype=np.float32)
        self.origin = disp(np.asarray(origin, dtype=float) - c) / k
        self.unit_m = k
        self.c = c

        # couleur de base : dégradé de hauteur ou texture/couleur du matériau, × éclairage, × grisé hors zone
        t = np.clip((self.pts[:, 1] + 1) / 2, 0, 1)[:, None]  # hauteur 0 (bas) -> 1 (haut)
        stops = np.array(theme.HEIGHT_RAMP, dtype=float)  # dégradé en 3 couleurs
        col = np.where(t < 0.5, stops[0] + (stops[1] - stops[0]) * (t * 2), stops[1] + (stops[2] - stops[1]) * (t * 2 - 1))
        lit = np.full(len(zup), 0.8) if normals is None else 0.3 + 0.7 * np.abs(normals[:, 2])
        if colors is not None:  # couleurs / textures réelles, éclairage plus doux pour les préserver
            has = ~np.isnan(colors[:, 0])
            col[has] = colors[has]
            lit = np.where(has, 0.7 + 0.3 * lit, lit)
        if self.outside is not None:
            lit = np.where(self.outside, lit * 0.2, lit)
        self.base = (col * lit[:, None]).astype(np.float32)

        self.render() if keep_view else self.reset()

    # ------------------------------------------------------------------ vues
    def reset(self):
        self.set_view("iso")

    def set_view(self, name):
        self.yaw, self.pitch = {"iso": (0.6, 0.45), "top": (0.0, self.TOP_PITCH),
                                "front": (0.0, 0.0), "side": (float(np.pi / 2), 0.0)}[name]
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        self.render()

    def _rot(self):
        """Lignes (x écran, y écran, profondeur) de la rotation courante, en float32."""
        cy, sy, cp, sp = np.cos(self.yaw), np.sin(self.yaw), np.cos(self.pitch), np.sin(self.pitch)
        return np.array([[cy, 0, sy],
                         [sp * sy, cp, -sp * cy],
                         [-cp * sy, sp, cp * cy]], dtype=np.float32)

    def _project(self, P, w, h):
        """P (N,3) normalisés -> (px, py, profondeur) en flottants (profondeur plus grande = plus proche)."""
        r = self._rot().astype(np.float64)
        x1, y2, z2 = (np.asarray(P, dtype=np.float64) @ r.T).T
        s = min(w, h) * 0.45 * self.zoom
        return w / 2 + (x1 + self.pan[0]) * s, h / 2 - (y2 + self.pan[1]) * s, z2

    # ------------------------------------------------------------------ rendu
    def request_render(self):
        """Rendu léger (geste en cours) à la prochaine image, puis rendu complet au repos."""
        self._interactive = True
        if self._sched is None:
            self._sched = self.after(1, self._frame)
        if self._settle is not None:
            self.after_cancel(self._settle)
        self._settle = self.after(SETTLE_MS, self._final)

    def _frame(self):
        self._sched = None
        self.render()

    def _final(self):
        self._settle = None
        self._interactive = False
        self.render()

    @staticmethod
    def _local_max(zb, w, gx, gy, r):
        """Profondeur maximale du modèle dans un carré (2r+1)² autour de chaque point (gx, gy)."""
        best = np.full(len(gx), -np.inf, dtype=np.float32)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                best = np.maximum(best, zb[(gy + dy) * w + gx + dx])
        return best

    def render(self):
        W, H = max(self.winfo_width(), 50), max(self.winfo_height(), 50)
        f = 2 if (W * H > 1_400_000 or (self._interactive and W * H > 500_000)) else 1
        w, h = W // f, H // f
        canvas = np.full(h * w, _pk(theme.VIEWPORT_RGB), dtype=np.uint32)
        if self.pts32 is not None:
            n = len(self.pts32) if not self._interactive else min(len(self.pts32), LOD_POINTS)
            P = self.pts32[:n] @ self._rot().T  # x écran, y écran, profondeur
            s = np.float32(min(w, h) * 0.45 * self.zoom)
            px = (w / 2 + (P[:, 0] + self.pan[0]) * s).astype(np.int32)
            py = (h / 2 - (P[:, 1] + self.pan[1]) * s).astype(np.int32)
            ok = np.nonzero((px >= 1) & (px < w - 2) & (py >= 1) & (py < h - 2))[0]
            z = P[ok, 2]
            # tri de profondeur 16 bits (radix) : loin d'abord, le plus proche écrase en dernier
            zq = ((z + 1.8) * np.float32(65535 / 3.6)).clip(0, 65535).astype(np.uint16)
            order = np.argsort(zq, kind="stable")
            idx, z = ok[order], z[order]
            lin = py[idx] * w + px[idx]
            shade = np.clip(0.85 + 0.15 * z, 0.5, 1.15)[:, None]
            rgba = np.empty((len(idx), 4), dtype=np.uint8)
            rgba[:, :3] = np.minimum(self.base[idx] * shade, 255)
            rgba[:, 3] = 255
            packed = rgba.view(np.uint32).ravel()
            zb = np.full(h * w, -np.inf, dtype=np.float32)  # profondeur du modèle (plus grand = plus proche)
            for off in ((0, 1, w, w + 1) if f == 1 else (0,)):  # points de 2 px à l'écran
                canvas[lin + off] = packed
            zb[lin] = z
            if len(idx):
                spacing = np.sqrt((np.ptp(px[idx]) + 1) * (np.ptp(py[idx]) + 1) / len(idx))
                self._overlays(canvas.reshape(h, w), zb, w, h, f, int(np.clip(np.ceil(spacing * 0.9), 1, 10)) + 1)
        rgb = np.ascontiguousarray(canvas.view(np.uint8).reshape(h, w, 4)[:, :, :3])
        self._img = tk.PhotoImage(data=b"P6 %d %d 255\n" % (w, h) + rgb.tobytes())
        if f > 1:
            self._img = self._img.zoom(f)
        self.configure(image=self._img)

    def _overlays(self, img, zb, w, h, f, r):
        """Repère XYZ (masqué par le modèle), boîte/polygone de recadrage, tracé en cours."""
        thick = (-1, 0, 1) if f == 1 else (-1, 0)

        def paint(sx, sy, colr, th=(0,)):
            px = _pk(colr)
            for dx in th:
                for dy in th:
                    img[sy + dy, sx + dx] = px

        def seg(p, q, colr, th=(0,)):
            a = np.array(self._project(np.array([p]), w, h)[:2]).ravel()
            b = np.array(self._project(np.array([q]), w, h)[:2]).ravel()
            n = int(max(2, min(400, np.hypot(*(b - a)))))
            tt = np.linspace(0, 1, n)[:, None]
            sx, sy, _ = self._project(np.asarray(p) + tt * (np.asarray(q) - np.asarray(p)), w, h)
            sx, sy = sx.astype(int), sy.astype(int)
            okk = (sx >= 2) & (sx < w - 2) & (sy >= 2) & (sy < h - 2)
            paint(sx[okk], sy[okk], colr, th)

        # repère de sortie (0,0,0) : X rouge, Y vert, Z bleu, caché là où le modèle passe devant
        if self.show_axes and self.origin is not None:
            ramp = np.linspace(0, self.GIZMO_LEN, 80)[:, None]
            for d, colr in (((1, 0, 0), theme.AXIS_X), ((0, 0, -1), theme.AXIS_Y), ((0, 1, 0), theme.AXIS_Z)):
                gx, gy, gd = self._project(self.origin + ramp * np.array(d, dtype=float), w, h)
                gx, gy = gx.astype(int), gy.astype(int)
                ok = (gx >= r + 2) & (gx < w - r - 2) & (gy >= r + 2) & (gy < h - r - 2)
                gx, gy, gd = gx[ok], gy[ok], gd[ok]
                vis = gd >= self._local_max(zb, w, gx, gy, r) - 0.02  # tolérance : un axe posé sur le sol reste visible
                paint(gx[vis], gy[vis], colr, thick)

        vw = lambda x, y, z: np.array([x - self.c[0], z - self.c[2], -(y - self.c[1])]) / self.unit_m
        if self.poly_mode and self.draft:  # tracé en cours
            zc = self.c[2]
            for a_, b_ in zip(self.draft, self.draft[1:]):
                seg(vw(*a_, zc), vw(*b_, zc), theme.CROP, (0, 1) if f == 1 else (0,))
            for i, (x_, y_) in enumerate(self.draft):
                vx, vy, _ = self._project(vw(x_, y_, zc)[None, :], w, h)
                vx, vy, rr = int(vx[0]), int(vy[0]), 3 if f == 1 else 2
                if rr < vx < w - rr and rr < vy < h - rr:
                    img[vy - rr:vy + rr + 1, vx - rr:vx + rr + 1] = _pk(theme.CLOSE if (i == 0 and len(self.draft) >= 3) else theme.CROP)
        elif self.poly is not None and self.crop is not None and len(self.poly) >= 3:  # polygone terminé : prisme
            z0, z1 = self.crop[0][2], self.crop[1][2]
            for ring in (z0, z1):
                for a_, b_ in zip(self.poly, self.poly[1:] + self.poly[:1]):
                    seg(vw(*a_, ring), vw(*b_, ring), theme.CROP)
            for x_, y_ in self.poly:
                seg(vw(x_, y_, z0), vw(x_, y_, z1), theme.CROP_DIM)
        elif self.crop is not None:  # boîte de recadrage
            lo, hi = (np.asarray(v, dtype=float) for v in self.crop)
            cn = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            cv = (np.stack([cn[:, 0], cn[:, 2], -cn[:, 1]], axis=1) - np.array([self.c[0], self.c[2], -self.c[1]])) / self.unit_m
            for i in range(8):
                for j in range(i + 1, 8):
                    if bin(i ^ j).count("1") == 1:  # une arête = deux coins qui diffèrent d'un seul axe
                        seg(cv[i], cv[j], theme.CROP)

    # ------------------------------------------------------------------ repère XYZ (poignée)
    GIZMO_LEN = 0.3
    # axes en repère d'affichage (Y vertical) : X, Y (profondeur inversée), Z (haut)
    AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 0.0, -1.0), "Z": (0.0, 1.0, 0.0)}

    def _size(self):
        return max(self.winfo_width(), 50), max(self.winfo_height(), 50)

    def _sdir(self, d):
        """Vecteur écran (px) d'un déplacement d'une unité normalisée le long de d."""
        w, h = self._size()
        px, py, _ = self._project(np.array([[0.0, 0, 0], d]), w, h)
        return np.array([px[1] - px[0], py[1] - py[0]])

    def _hit(self, x, y):
        """'C' (centre), 'X'/'Y'/'Z' (flèche) ou None."""
        if self.origin is None or self.pts is None or not self.show_axes:
            return None
        w, h = self._size()
        o = np.array(self._project(self.origin[None, :], w, h)[:2]).ravel()
        p = np.array([x, y], dtype=float)
        if np.hypot(*(p - o)) < 12:
            return "C"
        for name, d in self.AXES.items():
            v = self._sdir(np.array(d) * self.GIZMO_LEN)
            t = np.clip(np.dot(p - o, v) / max(np.dot(v, v), 1e-9), 0, 1)
            if np.hypot(*(p - (o + t * v))) < 8:
                return name
        return None

    def _hover(self, e):
        if self.pick_cb or self.poly_mode:
            return
        self.configure(cursor="hand2" if self._hit(e.x, e.y) else "fleur")

    # --- choix d'un point du modèle -------------------------------------------
    def start_pick(self, cb):
        self.pick_cb = cb
        self.configure(cursor="crosshair")

    def cancel_pick(self):
        self.pick_cb = None
        self.configure(cursor="fleur")

    def _pick(self, x, y):
        """Point du modèle sous le curseur (le plus proche de l'écran dans un rayon de quelques pixels)."""
        w, h = self._size()
        px, py, z2 = self._project(self.pts, w, h)
        for r in (6, 14, 30):
            near = np.nonzero((np.abs(px - x) <= r) & (np.abs(py - y) <= r))[0]
            if len(near):
                return self.zup[near[np.argmax(z2[near])]]
        return None

    # --- tracé d'un polygone de recadrage (vue de dessus) -----------------------
    TOP_PITCH = float(np.pi / 2)

    def start_polygon(self, on_done, on_status):
        self.cancel_pick()
        self.poly_mode, self.draft = True, []
        self.on_poly_done, self.on_poly_status = on_done, on_status
        self.set_view("top")
        self.configure(cursor="crosshair")
        self.focus_set()

    def _poly_end(self, status=""):
        self.poly_mode, self.draft = False, []
        self.configure(cursor="fleur")
        if self.on_poly_status:
            self.on_poly_status(status)
        self.render()

    def cancel_poly(self):
        if self.poly_mode:
            self._poly_end("")

    def undo_poly(self):
        if self.poly_mode and self.draft:
            self.draft.pop()
            self.render()

    def finish_poly(self):
        if not self.poly_mode:
            return
        if len(self.draft) < 3:
            if self.on_poly_status:
                self.on_poly_status("Il faut au moins 3 points pour fermer le polygone.")
            return
        pts, cb = list(self.draft), self.on_poly_done
        self._poly_end("")
        if cb:
            cb(pts)

    def _unproject(self, x, y):
        """Pixel -> (X, Y) en mètres, valable en vue de dessus (yaw 0, pitch 90°)."""
        W, H = self._size()
        s = min(W, H) * 0.45 * self.zoom
        x1 = (x - W / 2) / s - self.pan[0]
        y2 = (H / 2 - y) / s - self.pan[1]
        return (self.c[0] + x1 * self.unit_m, self.c[1] + y2 * self.unit_m)

    def _poly_click(self, x, y):
        if self.pts is None:
            return
        if len(self.draft) >= 3:  # clic sur le 1er point = fermer
            W, H = self._size()
            f0 = np.array([[self.draft[0][0] - self.c[0], 0.0, -(self.draft[0][1] - self.c[1])]]) / self.unit_m
            fx, fy, _ = self._project(f0, W, H)
            if np.hypot(fx[0] - x, fy[0] - y) <= 12:
                return self.finish_poly()
        self.draft.append(self._unproject(x, y))
        if self.on_poly_status:
            self.on_poly_status("%d point(s) — clique le 1er point (vert) ou Entrée pour fermer" % len(self.draft))
        self.render()

    # ------------------------------------------------------------------ souris
    # --- déplacement de la vue ------------------------------------------------
    def _pan_press(self, e):
        self._pan_last = (e.x, e.y)
        try:
            self.configure(cursor="closedhand")
        except tk.TclError:
            pass

    def _pan_drag(self, e):
        if self._pan_last is None:
            return
        s = min(*self._size()) * 0.45 * self.zoom
        self.pan[0] += (e.x - self._pan_last[0]) / s
        self.pan[1] -= (e.y - self._pan_last[1]) / s
        self._pan_last = (e.x, e.y)
        self.request_render()

    def _pan_release(self, e):
        self._pan_last = None
        self.configure(cursor="crosshair" if (self.pick_cb or self.poly_mode) else "fleur")

    def _press(self, e):
        if e.state & 0x5:  # Maj ou Ctrl + clic gauche = déplacer la vue (trackpad sans clic droit)
            return self._pan_press(e)
        if self.poly_mode:
            self._pstart, self._pmoved, self._last = (e.x, e.y), False, (e.x, e.y)
            return
        if self.pick_cb:
            cb, self.pick_cb = self.pick_cb, None
            self.configure(cursor="fleur")
            cb(self._pick(e.x, e.y) if self.pts is not None else None)
            return
        self._last = (e.x, e.y)
        hit = self._hit(e.x, e.y)
        self._grab = {"hit": hit, "start": (e.x, e.y), "o0": self.origin.copy()} if hit else None

    def _drag_gizmo(self, e):
        g = self._grab
        dp = np.array([e.x - g["start"][0], e.y - g["start"][1]], dtype=float)
        if g["hit"] == "C":  # plan horizontal : directions d'affichage X et -Y
            M = np.stack([self._sdir(np.array(self.AXES["X"])), self._sdir(np.array(self.AXES["Y"]))], axis=1)
            ab = np.clip(np.linalg.lstsq(M, dp, rcond=1e-3)[0], -3, 3)
            self.origin = g["o0"] + ab[0] * np.array(self.AXES["X"]) + ab[1] * np.array(self.AXES["Y"])
        else:
            d = np.array(self.AXES[g["hit"]])
            s = self._sdir(d)
            self.origin = g["o0"] + d * np.clip(np.dot(dp, s) / max(np.dot(s, s), 1e-9), -3, 3)
        self.request_render()

    def _release(self, e):
        if self._pan_last is not None:
            return self._pan_release(e)
        if self.poly_mode:
            if not self._pmoved:
                self._poly_click(e.x, e.y)
            return
        g, self._grab = self._grab, None
        if g and self.on_moved:
            dd = (self.origin - g["o0"]) * self.unit_m  # affichage (x, y vertical, z)
            if np.any(np.abs(dd) > 1e-9):
                self.on_moved((dd[0], -dd[2], dd[1]))  # -> repère Z vertical

    def _drag(self, e):
        if self._pan_last is not None:
            return self._pan_drag(e)
        if self.poly_mode:  # en mode polygone, glisser = déplacer la vue (un simple clic = ajouter un point)
            if np.hypot(e.x - self._pstart[0], e.y - self._pstart[1]) > 4:
                self._pmoved = True
            if self._pmoved:
                s = min(*self._size()) * 0.45 * self.zoom
                self.pan[0] += (e.x - self._last[0]) / s
                self.pan[1] -= (e.y - self._last[1]) / s
                self.request_render()
            self._last = (e.x, e.y)
            return
        if self.pick_cb:
            return
        if self._grab:
            return self._drag_gizmo(e)
        if self._last:
            self.yaw += (e.x - self._last[0]) * 0.01
            self.pitch = max(-1.55, min(1.55, self.pitch + (e.y - self._last[1]) * 0.01))
        self._last = (e.x, e.y)
        self.request_render()

    def _zoom(self, f, x=None, y=None):
        """Zoom ×f ; si (x, y) est donné, le point du modèle sous le curseur reste fixe à l'écran."""
        new = max(0.2, min(20, self.zoom * f))
        f = new / self.zoom
        if x is not None and f != 1:
            W, H = self._size()
            s = min(W, H) * 0.45 * self.zoom
            self.pan[0] += (x - W / 2) / s * (1 / f - 1)
            self.pan[1] += (H / 2 - y) / s * (1 / f - 1)
        self.zoom = new
        self.request_render()
