"""3D to AI — OBJ / topographic survey -> DAE (Collada) converter with a 3D preview.

Run :  python3 app.py
"""
import os
import queue
import threading
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

import obj2dae
import settings
import mapview
import theme
import topo
import updater
from preview3d import Preview

try:  # drag and drop if tkinterdnd2 is installed (pip install tkinterdnd2)
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _Root = TkinterDnD.Tk
except Exception:
    DND_FILES, _Root = None, tk.Tk

try:  # generated at build time by build_app.sh (V YY.MM.XX) ; absent when running the source code
    from _version import VERSION
except ImportError:
    VERSION = "dev"

PREVIEW_POINTS = 250_000
SIDE_W = 400  # width of the settings column


class Console(ttk.Label):
    """Message line under the 3D view, like the AI console ('>Add Stage')."""

    def __init__(self, master):
        super().__init__(master, style="Console.TLabel", anchor="w", justify="left")

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            kw["text"] = f">{kw['text']}" if kw["text"] else ""
        return super().configure(cnf, **kw)

    config = configure


class App(_Root):
    TABS = (("MODEL", "model"), ("PLACEMENT", "place"), ("CROP", "crop"), ("EXPORT", "export"))

    def __init__(self):
        super().__init__()
        self.title(f"3D to AI {VERSION} — OBJ / terrain → DAE")
        self.fam = theme.apply(self)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1280, int(sw * 0.92)), min(860, int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max((sh - h) // 3, 0)}")
        self.minsize(min(820, sw), min(520, sh))
        self.mesh = None
        self.src = None
        self._bounds = None
        self.poly = None  # crop polygon, points (h1, h2) on the horizontal output axes
        self.q = queue.Queue()
        self._pending = None
        self._rot_prev = (0.0, 0.0, 0.0)  # previous rotation : used to deduce the added rotation
        self._make_vars()
        self._build_ui()
        if DND_FILES:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", lambda e: self.load(self.tk.splitlist(e.data)[0]))
        self.bind("<Escape>", lambda e: self.cancel_pick())
        self.bind("<Return>", lambda e: self.preview.finish_poly())
        self.bind("<BackSpace>", lambda e: self.preview.undo_poly())
        # macOS : file dropped on the icon or 'Open With' in the Finder
        self.createcommand("::tk::mac::OpenDocument", self._open_documents)
        self.select_tab("model")
        self.after(100, self.poll)
        if self.auto_update.get() and VERSION != "dev":  # not for the source code ('dev')
            self.after(2500, lambda: self.check_update(manual=False))

    # --- variables --------------------------------------------------------------
    def _make_vars(self):
        self.unit = tk.StringVar(value="meters (m)")
        self.scale = tk.StringVar(value="1.0")
        self.src_up = tk.StringVar(value="Y")
        self.dst_up = tk.StringVar(value="Z")
        self.rot = [tk.StringVar(value="0") for _ in range(3)]
        self.off = [tk.StringVar(value="0") for _ in range(3)]
        self.center = tk.BooleanVar(value=False)
        self.floor = tk.BooleanVar(value=False)
        self.pick_z = tk.BooleanVar(value=True)
        self.crop_on = tk.BooleanVar(value=False)
        self.crop = {(ax, k): tk.StringVar(value="0") for ax in "XYZ" for k in ("min", "max")}
        self.show_tex = tk.BooleanVar(value=True)
        self.show_axes = tk.BooleanVar(value=True)
        self.single = tk.BooleanVar(value=True)
        self.normals = tk.BooleanVar(value=True)
        self.decim = tk.BooleanVar(value=True)  # triangle limit enabled by default
        self.target = tk.StringVar(value="1000")
        pr = settings.load()
        self.topo_place = tk.StringVar(value=pr.get("topo_place", ""))
        self.topo_size = tk.StringVar(value=str(pr.get("topo_size", 500)))
        self.topo_source = tk.StringVar(value=pr.get("topo_source") if pr.get("topo_source") in topo.SOURCES else topo.SOURCES[0])
        self.topo_kind = tk.StringVar(value=topo.KINDS[0])
        self.topo_exag = tk.StringVar(value="1")
        self.topo_poly = [tuple(p) for p in pr.get("topo_poly", [])] or None  # last polygon drawn
        self.topo_view = tuple(pr["topo_view"]) if pr.get("topo_view") else None  # (lat, lon, zoom) of the map
        self.topo_layer = pr.get("topo_layer") if pr.get("topo_layer") in topo.TILE_LAYERS else None  # (names from older versions are dropped)
        self.topo_tex = tk.BooleanVar(value=bool(pr.get("topo_texture", True)))
        self.auto_update = tk.BooleanVar(value=bool(settings.load().get("check_updates", True)))
        self._update = None  # last answer of the update check

    # --- small widget builders ------------------------------------------
    def _spin(self, parent, var, width=8, lo=-100000, hi=100000, inc=1, cb=None):
        cb = cb or self.schedule
        e = ttk.Spinbox(parent, textvariable=var, from_=lo, to=hi, increment=inc, width=width, command=cb)
        e.bind("<KeyRelease>", lambda ev: cb())
        return e

    def _combo(self, parent, var, values, width=14):
        c = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
        c.bind("<<ComboboxSelected>>", lambda e: self.schedule())
        return c

    @staticmethod
    def _section(parent, text, first=False):
        ttk.Label(parent, text=text.upper(), style="Section.TLabel").pack(anchor="w", pady=(0 if first else 14, 5))

    @staticmethod
    def _hint(parent, text):
        ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=SIDE_W - 50, justify="left").pack(
            anchor="w", pady=(4, 0))

    # --- interface ---------------------------------------------------------------
    def _build_ui(self):
        outer = ttk.Frame(self, style="Window.TFrame", padding=8)
        outer.pack(fill="both", expand=True)

        # main tab bar (large rectangular tabs, active one in white)
        top = ttk.Frame(outer, style="Window.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="3DtoAI", style="Logo.TLabel", width=13).pack(side="left", fill="y", padx=(0, 6))
        tabs = ttk.Frame(top, style="Window.TFrame")
        tabs.pack(side="left", fill="x", expand=True)
        self._tabs_frame = tabs
        self.upd_btn = ttk.Button(top, text="", style="Accent.TButton", command=self.open_update)  # shown when there is something new
        self.tab_btns = {}
        for i, (label, key) in enumerate(self.TABS):
            b = ttk.Button(tabs, text=label, style="Tab.TButton", command=lambda k=key: self.select_tab(k))
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            tabs.columnconfigure(i, weight=1, uniform="tab")
            self.tab_btns[key] = b

        body = ttk.Frame(outer, style="Window.TFrame")
        body.pack(fill="both", expand=True, pady=(8, 0))

        # ---- left column : title, scrolling content, fixed footer ----
        left_edge = ttk.Frame(body, style="Edge.TFrame", padding=1)
        left_edge.pack(side="left", fill="y")
        left = ttk.Frame(left_edge)
        left.pack(fill="both", expand=True)
        self.tab_title = tk.StringVar()
        ttk.Label(left, textvariable=self.tab_title, style="Title.TLabel", padding=(14, 10, 14, 4)).pack(
            side="top", anchor="w")
        footer = ttk.Frame(left, padding=(14, 8, 14, 12))  # always visible, whatever the height
        footer.pack(side="bottom", fill="x")
        ttk.Separator(left).pack(side="bottom", fill="x")

        canvas = tk.Canvas(left, width=SIDE_W, highlightthickness=0, bd=0, bg=theme.PANEL)
        vsb = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(canvas, padding=(14, 6, 14, 14))
        canvas.create_window((0, 0), window=side, anchor="nw", width=SIDE_W)
        self._canvas = canvas

        def fit(_=None):
            need, have = side.winfo_reqheight(), canvas.winfo_height()
            canvas.configure(scrollregion=(0, 0, SIDE_W, max(need, have)))
            if need > have:
                vsb.pack(side="left", fill="y")
            else:
                vsb.pack_forget()
                canvas.yview_moveto(0)

        side.bind("<Configure>", fit)
        canvas.bind("<Configure>", fit)

        # ---- right area : toolbar, 3D view, console ----
        right_edge = ttk.Frame(body, style="Edge.TFrame", padding=1)
        right_edge.pack(side="right", fill="both", expand=True, padx=(8, 0))
        right = ttk.Frame(right_edge, style="View.TFrame")
        right.pack(fill="both", expand=True)
        bar = ttk.Frame(right, style="Bar.TFrame", padding=(8, 6))
        bar.pack(side="top", fill="x")
        for text, view in (("Top", "top"), ("Front", "front"), ("Side", "side"), ("3D", "iso")):
            ttk.Button(bar, text=text, style="Tool.TButton", command=lambda v=view: self.preview.set_view(v)).pack(
                side="left", padx=(0, 4))
        ttk.Button(bar, text="Recenter", style="Tool.TButton", command=self._recenter).pack(side="left", padx=(8, 4))
        ttk.Checkbutton(bar, text="Textures", style="Toolbutton", variable=self.show_tex,
                        command=self.schedule).pack(side="right")
        ttk.Checkbutton(bar, text="XYZ frame", style="Toolbutton", variable=self.show_axes,
                        command=self._toggle_axes).pack(side="right", padx=(0, 4))
        cons = ttk.Frame(right, style="View.TFrame", padding=(10, 5, 10, 7))
        cons.pack(side="bottom", fill="x")
        self.lbl_pick = Console(cons)
        self.lbl_pick.pack(fill="x")
        cons.bind("<Configure>", lambda e: self.lbl_pick.configure(wraplength=max(e.width - 24, 100)))
        self.preview = Preview(right, self.on_gizmo_moved, self._commit_rot, lambda t: self.lbl_pick.config(text=t))
        self.preview.pack(side="top", fill="both", expand=True)

        # ---- tab contents ----
        self.pages = {k: ttk.Frame(side) for _, k in self.TABS}
        self._page_model(self.pages["model"])
        self._page_place(self.pages["place"])
        self._page_crop(self.pages["crop"])
        self._page_export(self.pages["export"])
        self._side = side

        # ---- footer : model limits, conversion, progress ----
        self.lbl_size = ttk.Label(footer, text="", style="Muted.TLabel", wraplength=SIDE_W - 30, justify="left")
        self.lbl_size.pack(anchor="w", pady=(0, 8))
        self.btn = ttk.Button(footer, text="Convert to .dae", style="Accent.TButton", command=self.convert,
                              state="disabled")
        self.btn.pack(fill="x")
        self.bar = ttk.Progressbar(footer, maximum=1.0, style="Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(8, 0))
        self.lbl_status = ttk.Label(footer, text="", style="Muted.TLabel", wraplength=SIDE_W - 30)
        self.lbl_status.pack(anchor="w", pady=(4, 0))

        def wheel(e):
            if side.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(int(-e.delta / 120) if abs(e.delta) >= 120 else -e.delta, "units")

        self.update_idletasks()
        for wdg in self._walk(side):
            wdg.bind("<MouseWheel>", wheel, add="+")

    def select_tab(self, key):
        self.cancel_pick()
        if getattr(self, "_tab", None) == key:  # re-selecting the active tab empties the page (Tk repaint)
            return
        self._tab = key
        for k, page in self.pages.items():
            page.pack_forget()
        self.pages[key].pack(fill="x")
        for k, b in self.tab_btns.items():
            b.configure(style="TabOn.TButton" if k == key else "Tab.TButton")
        self.tab_title.set({"model": "Model", "place": "Placement", "crop": "Crop", "export": "Export"}[key])
        self._canvas.yview_moveto(0)

    def _recenter(self):
        self.preview.zoom, self.preview.pan = 1.0, [0.0, 0.0]
        self.preview.render()

    def _toggle_axes(self):
        self.preview.show_axes = self.show_axes.get()
        self.preview.render()

    def _page_model(self, p):
        self._section(p, "Topographic survey (internet)", first=True)
        ttk.Button(p, text="Draw the area on the map…", style="Accent.TButton", command=self.open_map).pack(fill="x")
        self._hint(p, "Satellite map: you draw a polygon, only the terrain inside it is downloaded.")
        g = ttk.Frame(p)
        g.pack(fill="x", pady=(4, 0))
        g.columnconfigure(1, weight=1)
        ttk.Label(g, text="Source").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 10))
        self._combo(g, self.topo_source, list(topo.SOURCES), 30).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(g, text="Type").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 10))
        self._combo(g, self.topo_kind, list(topo.KINDS), 30).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(g, text="Relief ×").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 10))
        ttk.Entry(g, textvariable=self.topo_exag, width=9).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Checkbutton(p, text="Drape the satellite image (preview)", variable=self.topo_tex,
                        command=lambda: settings.save({**settings.load(), "topo_texture": self.topo_tex.get()})
                        ).pack(anchor="w", pady=(4, 0))
        self.lbl_topo = ttk.Label(p, text="", style="Muted.TLabel", wraplength=SIDE_W - 50)
        self.lbl_topo.pack(anchor="w", pady=(4, 0))

        ttk.Label(p, text="Or around an address:", style="Muted.TLabel").pack(anchor="w", pady=(10, 2))
        e = ttk.Entry(p, textvariable=self.topo_place)
        e.pack(fill="x")
        e.bind("<Return>", lambda ev: self.fetch_topo())
        r = ttk.Frame(p)
        r.pack(fill="x", pady=(4, 0))
        ttk.Label(r, text="Side (m)").pack(side="left", padx=(0, 8))
        ttk.Entry(r, textvariable=self.topo_size, width=7).pack(side="left")
        self.btn_topo = ttk.Button(r, text="Download", style="Tool.TButton", command=self.fetch_topo)
        self.btn_topo.pack(side="left", padx=(10, 0))
        self._hint(p, "Address, city, locality or 'latitude, longitude'. Data: IGN Géoplateforme (Open Licence, "
                      "France, 1 m step), AWS Terrain Tiles (SRTM/NASADEM…, worldwide), OpenStreetMap. The DAE is centered on "
                      "the area, the altitude of the lowest point is brought back to 0.")

        self._section(p, "Or an OBJ file")
        ttk.Button(p, text="Open an .obj…", style="Tool.TButton", command=self.open_dialog).pack(fill="x")
        self.lbl_file = ttk.Label(p, text="No file", wraplength=SIDE_W - 50, foreground=theme.WHITE)
        self.lbl_file.pack(anchor="w", pady=(8, 0))
        self.lbl_stats = ttk.Label(p, text="", style="Muted.TLabel", wraplength=SIDE_W - 50)
        self.lbl_stats.pack(anchor="w")

        self._section(p, "Units and axes")
        g = ttk.Frame(p)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        rows = (("File unit", self._combo(g, self.unit, list(obj2dae.UNITS_TO_METERS), 16)),
                ("Scale ×", None),
                ("Axe vertical source", self._combo(g, self.src_up, ["Y", "Z"], 6)),
                ("Axe vertical sortie", self._combo(g, self.dst_up, ["Y", "Z"], 6)))
        for r, (label, wdg) in enumerate(rows):
            ttk.Label(g, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            if wdg is None:
                wdg = ttk.Entry(g, textvariable=self.scale, width=9)
                wdg.bind("<KeyRelease>", lambda e: self.schedule())
            wdg.grid(row=r, column=1, sticky="w", pady=3)

        self._section(p, "Display")
        ttk.Checkbutton(p, text="Preview with textures / colours", variable=self.show_tex,
                        command=self.schedule).pack(anchor="w", pady=2)
        ttk.Checkbutton(p, text="Show the XYZ frame (AI origin)", variable=self.show_axes,
                        command=self._toggle_axes).pack(anchor="w", pady=2)
        self.lbl_tex = ttk.Label(p, text="", style="Muted.TLabel", wraplength=SIDE_W - 50)
        self.lbl_tex.pack(anchor="w", pady=(4, 0))
        self._section(p, "Version")
        ttk.Label(p, text=f"3D to AI {VERSION}", foreground=theme.WHITE).pack(anchor="w")
        self.lbl_update = ttk.Label(p, text="", style="Muted.TLabel", wraplength=SIDE_W - 50)
        self.lbl_update.pack(anchor="w", pady=(2, 6))
        ur = ttk.Frame(p)
        ur.pack(fill="x")
        ttk.Button(ur, text="Check for updates", command=lambda: self.check_update(manual=True)).pack(side="left")
        ttk.Checkbutton(p, text="Check at startup", variable=self.auto_update,
                        command=self._save_prefs).pack(anchor="w", pady=(6, 0))
        self._hint(p, "3D view: drag = rotate · right-click + drag (or Shift + drag) = pan · wheel = zoom "
                      "towards the cursor · double-click = recenter. "
                      "Frame rings = rotation around the axis. The XYZ frame is the AI origin (blue Z = Top, green Y = Court, red X = Nose): grab its centre or an arrow "
                      "and drag to move the model.")

    def _xyz_row(self, p, vars_, width=9, header=True, cb=None):
        """Row of three X / Y / Z fields with grey headers like an AI table."""
        g = ttk.Frame(p)
        g.pack(fill="x")
        for c, name in enumerate("XYZ"):
            g.columnconfigure(c, weight=1, uniform="xyz")
            if header:
                ttk.Label(g, text=name, style="Header.TLabel").grid(row=0, column=c, sticky="ew", padx=(0 if c == 0 else 2, 0))
            e = self._spin(g, vars_[c], width, cb=cb)
            e.grid(row=1, column=c, sticky="ew", padx=(0 if c == 0 else 2, 0), pady=(2, 0))
        return g

    def _page_place(self, p):
        self._section(p, "Rotation in 90° steps (around the XYZ frame)", first=True)
        g = ttk.Frame(p)
        g.pack(fill="x")
        g.columnconfigure((1, 2), weight=1, uniform="b")
        for i, axis in enumerate("XYZ"):
            ttk.Label(g, text=axis, style="Header.TLabel", width=3).grid(row=i, column=0, sticky="nsew", pady=1)
            ttk.Button(g, text="−90°", command=lambda i=i: self.rot90(i, -90)).grid(row=i, column=1, sticky="ew", padx=(2, 0), pady=1)
            ttk.Button(g, text="+90°", command=lambda i=i: self.rot90(i, 90)).grid(row=i, column=2, sticky="ew", padx=(2, 0), pady=1)

        self._hint(p, "The model stays fixed on screen: it is the XYZ frame (the AI origin) that pivots. ±90° buttons, or grab "
                      "a ring (red = X, green = Y, blue = Z) of the frame in the 3D view and drag: magnet every 15°.")

        self._section(p, "Fine adjustment (degrees)")
        self._xyz_row(p, self.rot, cb=self.on_rot_change)
        b = ttk.Frame(p)
        b.pack(fill="x", pady=(8, 0))
        for c, (text, cmd) in enumerate((("Level ground", self.do_level), ("Align on X", self.do_yaw),
                                          ("Rot. = 0", lambda: self.set_rot(0, 0, 0)))):
            b.columnconfigure(c, weight=1, uniform="rb")
            ttk.Button(b, text=text, command=cmd).grid(row=0, column=c, sticky="ew", padx=(0 if c == 0 else 3, 0))

        self._section(p, "Position (meters)")
        self._xyz_row(p, self.off)
        ttk.Checkbutton(p, text="Center the model in X/Y on the origin", variable=self.center,
                        command=self.schedule).pack(anchor="w", pady=(8, 2))
        ttk.Checkbutton(p, text="Put the lowest point at Z = 0", variable=self.floor,
                        command=self.schedule).pack(anchor="w", pady=2)
        r = ttk.Frame(p)
        r.pack(fill="x", pady=(8, 0))
        ttk.Button(r, text="Origin = clicked point", command=self.pick_origin).pack(side="left")
        ttk.Checkbutton(r, text="with Z", variable=self.pick_z).pack(side="left", padx=10)

    def _page_crop(self, p):
        ttk.Checkbutton(p, text="Crop: keep only this area", variable=self.crop_on,
                        command=self.on_crop_toggle).pack(anchor="w")
        self._section(p, "Area (meters)")
        g = ttk.Frame(p)
        g.pack(fill="x")
        for c, name in enumerate(("", "Min", "Max")):
            g.columnconfigure(c, weight=1 if c else 0)
            if name:
                ttk.Label(g, text=name, style="Header.TLabel").grid(row=0, column=c, sticky="ew", padx=(2, 0))
        for r, axis in enumerate("XYZ", start=1):
            ttk.Label(g, text=axis, style="Header.TLabel", width=3).grid(row=r, column=0, sticky="nsew", pady=1)
            for c, kind in enumerate(("min", "max"), start=1):
                self._spin(g, self.crop[(axis, kind)], 9).grid(row=r, column=c, sticky="ew", padx=(2, 0), pady=1)
        self._section(p, "Tools")
        b = ttk.Frame(p)
        b.pack(fill="x")
        b.columnconfigure((0, 1), weight=1, uniform="cb")
        for i, (text, cmd) in enumerate((("Area = whole model", self.crop_reset),
                                          ("Center on a click", self.pick_crop_center),
                                          ("Draw a polygon", self.start_polygon),
                                          ("Clear the polygon", self.clear_poly))):
            ttk.Button(b, text=text, command=cmd).grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 3, 0),
                                                       pady=(0 if i < 2 else 3, 0))
        self._hint(p, "The polygon is drawn in top view: click = point, click on the 1st point (green) or Enter = "
                      "close, Backspace = undo the last one, Esc = give up. The excluded part is greyed ; "
                      "the kept area is drawn in orange.")

    def _page_export(self, p):
        self._section(p, "AI compatibility", first=True)
        ttk.Checkbutton(p, text="A single triangle group (required by AI)", variable=self.single).pack(anchor="w", pady=2)
        ttk.Checkbutton(p, text="Include the normals", variable=self.normals).pack(anchor="w", pady=2)
        self._section(p, "Simplification")
        ttk.Checkbutton(p, text="Reduce the number of triangles", variable=self.decim).pack(anchor="w", pady=2)
        r = ttk.Frame(p)
        r.pack(fill="x", pady=(4, 0))
        ttk.Label(r, text="Target").pack(side="left", padx=(24, 8))
        ttk.Entry(r, textvariable=self.target, width=11).pack(side="left")
        self._hint(p, "AI reads a single triangle group and can run out of memory on very large meshes: "
                      "reduce them (1,000 by default: raise the target if the result is too coarse).")

    # --- updates ----------------------------------------------------------
    def _save_prefs(self):
        settings.save({**settings.load(), "check_updates": bool(self.auto_update.get())})

    def check_update(self, manual=False):
        """Query GitHub in the background (the interface never freezes)."""
        if manual:
            self.lbl_update.config(text="Checking…")

        def work():
            try:
                self.q.put(("update", updater.fetch_latest(VERSION), manual))
            except Exception as e:  # noqa: BLE001  (no network, GitHub unreachable, rate limit…)
                self.q.put(("update_err", str(e), manual))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, info, manual):
        self._update = info
        tag = info["tag"]
        if info["newer"]:
            self.lbl_update.config(text=f"New version available: {tag} (published {info['date']}).",
                                   foreground=theme.BROWN)
            self.upd_btn.config(text=f"Update {tag}")
            self.upd_btn.pack(side="right", padx=(6, 0), before=self._tabs_frame)
            if manual and messagebox.askyesno(
                    "Update available",
                    f"{tag} is available (current version: {VERSION}).\n\n{info['notes'][:600]}\n\n"
                    "Open the download page?"):
                self.open_update()
            return
        self.upd_btn.pack_forget()
        if VERSION == "dev":
            self.lbl_update.config(text=f"Development version (source code). Latest published version: {tag}.",
                                   foreground=theme.MUTED)
        else:
            self.lbl_update.config(text=f"You have the latest version ({tag}).", foreground=theme.MUTED)
        if manual:
            messagebox.showinfo("Update", self.lbl_update.cget("text"))

    def _on_update_error(self, err, manual):
        if manual:  # at startup, a failure stays silent
            self.lbl_update.config(text="Could not reach GitHub (no connection?).", foreground=theme.ERROR)
            messagebox.showwarning("Update", f"Could not check the latest version.\n\n{err}")

    def open_update(self):
        if self._update:
            webbrowser.open(self._update["download"] if self._update["newer"] else self._update["page"])

    def _open_documents(self, *paths):
        for p in paths:
            if p.lower().endswith(".obj"):
                return self.load(p)

    @staticmethod
    def _walk(w):
        yield w
        for c in w.winfo_children():
            yield from App._walk(c)

    # --- loading -------------------------------------------------------
    def open_dialog(self):
        p = filedialog.askopenfilename(title="Choose an OBJ", filetypes=[("Wavefront OBJ", "*.obj")])
        if p:
            self.load(p)

    def open_map(self):
        pr = settings.load()
        view = self.topo_view or (46.6, 2.5, 6)
        dlg = mapview.MapDialog(self, self._on_map_ok, view, self.topo_poly, self.topo_layer, pr.get("topo_label", ""))
        dlg.search.set(self.topo_place.get())

    def _on_map_ok(self, poly, view, label, layer):
        self.topo_poly, self.topo_view, self.topo_layer = poly, view, layer
        prefs = settings.load()
        prefs.update(topo_poly=[list(p) for p in poly], topo_view=list(view), topo_layer=layer, topo_label=label)
        settings.save(prefs)
        self.fetch_topo(poly, label)

    def fetch_topo(self, poly=None, label=None):
        """Download the terrain : of a polygon drawn on the map, otherwise of a square around the typed address."""
        try:
            size = float(self.topo_size.get().replace(",", "."))
            exag = float(self.topo_exag.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning("Topographic survey", "Invalid area size or relief factor.")
            return
        place, source, kind = self.topo_place.get().strip(), self.topo_source.get(), self.topo_kind.get()
        prefs = settings.load()
        prefs.update(topo_place=place, topo_size=size, topo_source=source, topo_texture=self.topo_tex.get())
        tex = (self.topo_layer if self.topo_layer in topo.TILE_LAYERS else next(iter(topo.TILE_LAYERS))) \
            if self.topo_tex.get() else None
        settings.save(prefs)
        self.src = None
        self.mesh = None
        self.btn.config(state="disabled")
        self.btn_topo.config(state="disabled")
        self.lbl_file.config(text=label or place or "Topographic survey")
        self.lbl_stats.config(text="Downloading…")
        self.lbl_topo.config(text="")

        def work():
            try:
                prog = lambda f, t: self.q.put(("prog", f, t))  # noqa: E731
                if poly:
                    m, info = topo.build_polygon(poly, label or "Drawn area", source, kind, 0.0, exag, prog, tex)
                else:
                    m, info = topo.build(place, size, source, kind, 0.0, exag, prog, tex)
                self.q.put(("loaded", m, "Z", info))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def load(self, path):
        if not path.lower().endswith(".obj"):
            messagebox.showwarning("File", "This is not an .obj file")
            return
        self.src = path
        self.mesh = None
        self.btn.config(state="disabled")
        self.lbl_file.config(text=os.path.basename(path))
        self.lbl_stats.config(text="Reading…")

        def work():
            try:
                m = obj2dae.load_obj(path, lambda f, t: self.q.put(("prog", f, t)))
                self.q.put(("loaded", m))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _floats(vars_):
        try:
            return tuple(float(v.get().replace(",", ".")) for v in vars_)
        except ValueError:
            return (0.0, 0.0, 0.0)

    def get_rot(self):
        return self._floats(self.rot)

    def get_off(self):
        return self._floats(self.off)

    def get_scale(self):
        return obj2dae.UNITS_TO_METERS[self.unit.get()] * float(self.scale.get().replace(",", "."))

    @staticmethod
    def _fmt(x, nd=2):
        s = f"{x:.{nd}f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"

    def set_rot(self, rx, ry, rz):
        for v, x in zip(self.rot, (rx, ry, rz)):
            v.set(self._fmt(x))
        self.on_rot_change()

    def _rot_values(self):
        """Angles of the rotation fields, or None if a field is empty / incomplete (typing in progress)."""
        try:
            return tuple(float(v.get().replace(",", ".")) for v in self.rot)
        except ValueError:
            return None

    def on_rot_change(self):
        """The rotation is made around the XYZ frame (AI origin) : the model turns like a solid around
        that origin. The added rotation is therefore also applied to the position (offset), and to the crop zone
        when possible, so that the point placed on the origin stays there."""
        new = self._rot_values()
        if new is None:
            return
        D = obj2dae.rotation_matrix(*new) @ obj2dae.rotation_matrix(*self._rot_prev).T  # added rotation (Z-up)
        self._rot_prev = new
        if not np.allclose(D, np.eye(3), atol=1e-12):
            P = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float) if self.dst_up.get() == "Y" else np.eye(3)
            Do = P @ D @ P.T  # same rotation in the output frame
            for var, x in zip(self.off, Do @ np.asarray(self.get_off())):
                var.set(self._fmt(x, 3))
            self._follow_rotation(Do)
        self.schedule()

    def _fit_crop_to_poly(self):
        """The crop box takes the horizontal extent of the polygon."""
        for k, ax_i in enumerate(self._hor()):
            vals = [p[k] for p in self.poly]
            ax = "XYZ"[ax_i]
            self.crop[(ax, "min")].set(f"{np.floor(min(vals) * 100) / 100:g}")
            self.crop[(ax, "max")].set(f"{np.ceil(max(vals) * 100) / 100:g}")

    def _follow_rotation(self, Do):
        """The crop zone follows the model for a rotation around the vertical (any angle for a
        polygon, multiple of 90° for the box). For another rotation, it stays in place."""
        up = 2 if self.dst_up.get() == "Z" else 1
        if abs(Do[up, up] - 1) > 1e-9:
            return
        hor = self._hor()
        M = Do[np.ix_(hor, hor)]
        if self.poly:
            self.poly = [tuple(M @ np.asarray(p, dtype=float)) for p in self.poly]
            self._fit_crop_to_poly()
        elif self.crop_on.get() and self.get_crop() is not None:
            quarter = np.degrees(np.arctan2(M[1, 0], M[0, 0])) / 90
            if abs(quarter - round(quarter)) > 1e-6:
                return
            axes = ["XYZ"[i] for i in hor]
            lo = np.array([float(self.crop[(ax, "min")].get().replace(",", ".")) for ax in axes])
            hi = np.array([float(self.crop[(ax, "max")].get().replace(",", ".")) for ax in axes])
            corners = np.array([lo, hi, [lo[0], hi[1]], [hi[0], lo[1]]]) @ M.T
            for k, ax in enumerate(axes):
                self.crop[(ax, "min")].set(self._fmt(corners[:, k].min()))
                self.crop[(ax, "max")].set(self._fmt(corners[:, k].max()))

    def rot90(self, axis, deg):
        """Rotation step around the XYZ frame : the frame pivots on screen around the axis, then it is committed."""
        self.preview.animate_rotation(axis, deg, self._commit_rot)

    def _commit_rot(self, axis, deg):
        """Commit a rotation made on screen (ring or button) : added around the axis `axis` of the frame."""
        self.set_rot(*obj2dae.rotate_world(self.get_rot(), axis, deg))

    def do_level(self):
        if self.mesh is None:
            return
        z = obj2dae.to_zup(self.mesh.verts, self.src_up.get())
        rx, ry = obj2dae.auto_level(z)
        self.set_rot(rx, ry, self.get_rot()[2])

    def do_yaw(self):
        if self.mesh is None:
            return
        rx, ry, _ = self.get_rot()
        z = obj2dae.rotate(obj2dae.to_zup(self.mesh.verts, self.src_up.get()), (rx, ry, 0))
        self.set_rot(rx, ry, obj2dae.auto_yaw(z))

    def on_gizmo_moved(self, d):
        """The frame was dragged by d (m, Z-up frame) : the model moves by -d relative to it."""
        mv = -np.asarray(d, dtype=float)
        if self.dst_up.get() == "Y":  # Z-up frame of the preview -> output frame
            mv = np.array([mv[0], mv[2], -mv[1]])
        for var, x in zip(self.off, np.asarray(self.get_off()) + mv):
            var.set(self._fmt(x, 3))
        if self.poly:
            i1, i2 = self._hor()
            self.poly = [(p[0] + mv[i1], p[1] + mv[i2]) for p in self.poly]
        if self.crop_on.get() and self.get_crop() is not None:  # the zone follows the model
            for i, ax in enumerate("XYZ"):
                for kind in ("min", "max"):
                    var = self.crop[(ax, kind)]
                    var.set(f"{float(var.get().replace(',', '.')) + mv[i]:.2f}")
        self.schedule()

    # --- crop / placement by click ------------------------------------
    def get_crop(self):
        """((xmin,ymin,zmin),(xmax,ymax,zmax)) in the output frame, or None if disabled / invalid."""
        if not self.crop_on.get():
            return None
        try:
            v = {k: float(var.get().replace(",", ".")) for k, var in self.crop.items()}
        except ValueError:
            return None
        lo = tuple(min(v[(a, "min")], v[(a, "max")]) for a in "XYZ")
        hi = tuple(max(v[(a, "min")], v[(a, "max")]) for a in "XYZ")
        if self.poly:
            return lo, hi, [tuple(p) for p in self.poly]
        return lo, hi

    def crop_reset(self):
        """Area = current limits of the placed model (also clears the polygon)."""
        if self._bounds is None:
            return
        self.poly = None
        lo, hi = self._bounds
        for i, ax in enumerate("XYZ"):
            self.crop[(ax, "min")].set(f"{np.floor(lo[i] * 100) / 100:g}")
            self.crop[(ax, "max")].set(f"{np.ceil(hi[i] * 100) / 100:g}")
        self.schedule()

    def on_crop_toggle(self):
        if self.crop_on.get() and all(float(v.get() or 0) == 0 for v in self.crop.values()):
            self.crop_reset()
        self.schedule()

    def _to_out(self, p):
        """Point of the preview frame (Z-up) -> output frame."""
        p = np.asarray(p, dtype=float)
        return np.array([p[0], p[2], -p[1]]) if self.dst_up.get() == "Y" else p

    def cancel_pick(self):
        self.preview.cancel_pick()
        self.preview.cancel_poly()
        self.lbl_pick.config(text="")

    # --- crop polygon ----------------------------------------------
    def _hor(self):
        """Indices of the horizontal axes of the output frame."""
        return (0, 1) if self.dst_up.get() == "Z" else (0, 2)

    def _poly_to_zup(self):
        if not self.poly:
            return None
        return [(h1, h2) if self.dst_up.get() == "Z" else (h1, -h2) for h1, h2 in self.poly]

    def start_polygon(self):
        if self.mesh is None:
            return
        self.cancel_pick()
        if self._bounds is not None and self.get_crop() is None:
            self.crop_on.set(True)
            if all(float(v.get() or 0) == 0 for v in self.crop.values()):
                self.crop_reset()
        self.lbl_pick.config(text="Top view: click to place the points · click the 1st point (green) or press Enter "
                                  "to close · Backspace = undo the last · Esc = give up · "
                                  "drag = pan the view · wheel = zoom")
        self.preview.start_polygon(self._on_poly_done, lambda t: self.lbl_pick.config(text=t))

    def _on_poly_done(self, pts):
        """pts : points (X, Y) in Z-up frame -> output frame ; the box takes the extent of the polygon."""
        self.poly = [(x, y) if self.dst_up.get() == "Z" else (x, -y) for x, y in pts]
        self._fit_crop_to_poly()
        self.crop_on.set(True)
        self.lbl_pick.config(text=f"Polygon of {len(self.poly)} points saved.")
        self.schedule()

    def clear_poly(self):
        self.poly = None
        self.lbl_pick.config(text="")
        self.schedule()

    def pick_origin(self):
        self.lbl_pick.config(text="Click a point of the model to make it the origin (Esc = cancel)")
        self.preview.start_pick(self._on_pick_origin)

    def _on_pick_origin(self, p):
        self.lbl_pick.config(text="")
        if p is None:
            self.lbl_pick.config(text="No model point at this place.")
            return
        p = np.array(p, dtype=float)
        if not self.pick_z.get():
            p[2] = 0.0  # keep the current height
        self.on_gizmo_moved(p)  # the frame goes to the point : the model moves by -p

    def pick_crop_center(self):
        if self.get_crop() is None and self._bounds is not None:
            self.crop_on.set(True)
            self.crop_reset()
        self.lbl_pick.config(text="Click the centre of the area to keep (Esc = cancel)")
        self.preview.start_pick(self._on_pick_crop)

    def _on_pick_crop(self, p):
        self.lbl_pick.config(text="")
        if p is None:
            self.lbl_pick.config(text="No model point at this place.")
            return
        lo, hi = self.get_crop()
        c = self._to_out(p)
        for i, ax in enumerate("XYZ"):
            if ax == "Z":
                continue  # the height of the zone stays unchanged
            half = (hi[i] - lo[i]) / 2
            self.crop[(ax, "min")].set(f"{c[i] - half:.2f}")
            self.crop[(ax, "max")].set(f"{c[i] + half:.2f}")
        self.schedule()

    def tex_status(self, n_tex, cols):
        m = self.mesh
        wanted = [p for p in (m.mat_tex or {}).values()]
        missing = [os.path.basename(p) for p in wanted if obj2dae.load_texture(p) is None]
        if n_tex:
            return f"Textures shown: {n_tex} image(s)" + (f" · missing: {', '.join(missing)}" if missing else "")
        if missing:
            return "Image(s) not found next to the OBJ: " + ", ".join(missing)
        if not np.isnan(cols[:, 0]).all():
            return "Material colours (.mtl) shown"
        return "No texture or colour in this OBJ (.mtl missing?) — colours = height"

    def schedule(self, keep_view=True):
        """Recompute preview + dimensions after a short pause (avoids recomputing at every keystroke)."""
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(120, lambda: self.refresh(keep_view))

    def refresh(self, keep_view=True):
        self._pending = None
        if self.mesh is None:
            return
        try:
            k = self.get_scale()
        except ValueError:
            self.lbl_size.config(text="Invalid scale factor")
            return
        dst = self.dst_up.get()
        v = obj2dae.place(self.mesh.verts, k, self.src_up.get(), dst, self.get_rot(),
                          self.center.get(), self.floor.get(), self.get_off())
        lo, hi = v.min(axis=0), v.max(axis=0)
        self._bounds = (lo, hi)
        self.lbl_size.config(text=(
            "Output position (m), %s-up frame:\n"
            "  X : %.2f → %.2f   (%.1f)\n  Y : %.2f → %.2f   (%.1f)\n  Z : %.2f → %.2f   (%.1f)"
            % (dst, lo[0], hi[0], hi[0] - lo[0], lo[1], hi[1], hi[1] - lo[1], lo[2], hi[2], hi[2] - lo[2])))
        zup = obj2dae.to_zup(v, "Y") if dst == "Y" else v  # the preview is always Z-up
        pts, nrm, idx, bary = obj2dae.sample_surface(zup, self.mesh.tris, PREVIEW_POINTS, with_bary=True)
        cols = None
        if self.show_tex.get():
            cols, n_tex = obj2dae.surface_colors(self.mesh, idx, bary)
            self.lbl_tex.config(text=self.tex_status(n_tex, cols))
        else:
            self.lbl_tex.config(text="")
        box = self.get_crop()
        if box is not None:
            box = box[:2]  # (lo, hi) ; the polygon, if any, is passed separately
            if dst == "Y":  # box in output frame -> preview frame (Z-up)
                (x0, y0, z0), (x1, y1, z1) = box
                box = ((x0, -z1, y0), (x1, -z0, y1))
        poly = self._poly_to_zup() if box is not None else None
        self.preview.set_points(pts, nrm, cols, keep_view=keep_view, crop=box, poly=poly,
                                 frame=obj2dae.rotation_matrix(*self.get_rot()))

    # --- conversion -------------------------------------------------------
    def convert(self):
        try:
            k = self.get_scale()
            tgt = int(self.target.get()) if self.decim.get() else None
        except ValueError:
            messagebox.showerror("Settings", "Invalid scale factor or target.")
            return
        dst = filedialog.asksaveasfilename(
            title="Save the DAE", defaultextension=".dae",
            initialfile=(os.path.splitext(os.path.basename(self.src))[0] if self.src
                         else (self.lbl_file.cget("text") or "survey").strip().replace("/", "-") or "survey") + ".dae",
            filetypes=[("Collada", "*.dae")])
        if not dst:
            return
        self.btn.config(state="disabled")
        args = dict(unit_factor=k, src_up=self.src_up.get(), dst_up=self.dst_up.get(),
                    center=self.center.get(), target_tris=tgt, with_normals=self.normals.get(),
                    rot=self.get_rot(), floor=self.floor.get(), offset=self.get_off(),
                    single_group=self.single.get(), crop=self.get_crop())

        def work():
            try:
                st = obj2dae.convert(self.src, dst, progress=lambda f, t: self.q.put(("prog", f, t)),
                                    mesh=None if self.src else self.mesh, **args)
                self.q.put(("done", dst, st))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "prog":
                    self.bar["value"] = msg[1]
                    self.lbl_status.config(text=msg[2])
                elif kind == "loaded":
                    self.mesh = msg[1]
                    nt = len(self.mesh.tris)
                    self.lbl_stats.config(text=f"{len(self.mesh.verts):,} vertices · {nt:,} triangles")
                    self.bar["value"] = 0
                    self.lbl_status.config(text="")
                    self.btn.config(state="normal")
                    self.btn_topo.config(state="normal")
                    if len(msg) > 2:  # topographic survey : meters, Z-up, centered on the place
                        info = msg[3]
                        self.src_up.set(msg[2])
                        self.unit.set("meters (m)")
                        self.scale.set("1.0")
                        self.floor.set(True)
                        self.lbl_file.config(text=info["label"].split(",")[0])
                        self.lbl_topo.config(text="%s · step ≈ %.1f m · altitude %.0f → %.0f m · extent %.0f × %.0f m · %.2f ha" % (
                            info["source"], info["step"], info["zmin"], info["zmax"], *info["size"], info["area"] / 1e4)
                            + (f"\nSatellite image: {info['texture']}" if info.get("texture") else "")
                            + (f"\nSatellite image unavailable: {info['texture_error']}" if info.get("texture_error") else ""))
                    else:
                        self.src_up.set(obj2dae.guess_up_axis(self.mesh.verts))
                    self.refresh(keep_view=False)
                elif kind == "done":
                    st = msg[2]
                    self.bar["value"] = 1
                    self.btn.config(state="normal")
                    self.lbl_status.config(text="Saved ✔")
                    messagebox.showinfo(
                        "Conversion finished",
                        f"{msg[1]}\n\nTriangles: {st['tris_in']:,} → {st['tris_out']:,}\n"
                        "Dimensions: %.1f × %.1f × %.1f m" % tuple(st["size_m"]))
                elif kind == "update":
                    self._on_update_result(msg[1], msg[2])
                elif kind == "update_err":
                    self._on_update_error(msg[1], msg[2])
                elif kind == "error":
                    self.btn_topo.config(state="normal")
                    self.btn.config(state="normal" if self.mesh else "disabled")
                    self.lbl_status.config(text="Error")
                    self.lbl_stats.config(text="")
                    messagebox.showerror("Error", msg[1])
        except queue.Empty:
            pass
        self.after(100, self.poll)


def _sample_obj(folder, n=30):
    """Synthetic OBJ (wavy sheet) for the self-tests."""
    path = os.path.join(folder, "t.obj")
    with open(path, "w") as f:
        for i in range(n):
            for j in range(n):
                f.write(f"v {i} {j} {np.sin(i / 5) * 2:.3f}\n")
        for i in range(n - 1):
            for j in range(n - 1):
                a = i * n + j + 1
                f.write(f"f {a} {a + 1} {a + n + 1}\nf {a} {a + n + 1} {a + n}\n")
    return path


def selftest():
    """Check the conversion engine without a window (useful to validate a build)."""
    import tempfile
    import xml.etree.ElementTree as ET

    with tempfile.TemporaryDirectory() as d:
        obj = _sample_obj(d)
        dae = os.path.join(d, "t.dae")
        st = obj2dae.convert(obj, dae, 1.0, "Z", "Z", True, None, True, (0, 0, 90), True, (1, 2, 3), True,
                             crop=((-100, -100, -100), (100, 100, 100), [(-10, -10), (10, -10), (10, 10), (-10, 10)]))
        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        mesh = ET.parse(dae).getroot().find(".//c:geometry/c:mesh", ns)
        groups = mesh.findall("c:triangles", ns)
        assert len(groups) == 1, "AI requires a single triangle group"
        assert len(groups[0].findall("c:input", ns)) == 1, "AI requires a single input per vertex"
        assert 0 < st["tris_cropped"] < st["tris_in"], "the polygon crop filtered nothing"
    assert updater.is_newer("V26.10.04", "V26.10.03") and updater.is_newer("V26.11.01", "V26.10.12")
    assert not updater.is_newer("V26.10.03", "V26.10.03") and not updater.is_newer("V26.10.02", "dev")
    print("OK :", VERSION, "- OBJ->DAE engine working,", st["tris_in"], "->", st["tris_cropped"], "triangles")
    return 0


def smoketest(shot=None):
    """Really opens the interface : loads a model, goes through the tabs and views, renders the preview,
    exports a DAE. `shot` : path of a PNG screenshot (needs Pillow and a screen)."""
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as d:
        app = App()
        app.geometry("1200x800+30+30")
        app.update()
        app.load(_sample_obj(d))
        t0 = time.time()
        while app.mesh is None and time.time() - t0 < 30:
            app.update()
            time.sleep(0.05)
        assert app.mesh is not None, "loading the OBJ failed"
        for key in ("place", "crop", "export", "model"):
            app.select_tab(key)
            app.update()
        app.rot90(2, 90)
        for view in ("top", "front", "side", "iso"):
            app.preview.set_view(view)
            app.update()
        app.preview._interactive = False
        app.preview.render()
        for _ in range(10):
            app.update()
            time.sleep(0.05)
        out = os.path.join(d, "t.dae")
        app.update_idletasks()
        st = obj2dae.convert(app.src, out, app.get_scale(), app.src_up.get(), app.dst_up.get(), False, None, True,
                             app.get_rot(), False, app.get_off(), True)
        assert os.path.getsize(out) > 0 and st["tris_out"] > 0
        if shot:
            from PIL import ImageGrab
            ImageGrab.grab().save(shot)
        # update interface (simulated answer : no network access)
        fake = {"tag": "V99.01.01", "name": "x", "date": "2099-01-01", "notes": "", "page": "p", "download": "d", "newer": True}
        app._on_update_result(fake, False)
        app.update()
        assert app.upd_btn.winfo_ismapped(), "the update button does not appear"
        app._on_update_result({**fake, "tag": "V00.01.01", "newer": False}, False)
        app.update()
        assert not app.upd_btn.winfo_ismapped(), "the update button does not disappear"
        app.destroy()
    print("OK :", VERSION, "- interface working (loading, tabs, views, rendering, export)")
    return 0


def main(argv):
    import sys

    for stream in (sys.stdout, sys.stderr):  # Windows console (cp1252) : never crash on an exotic character
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001  (app without console : stream absent)
            pass
    if "--selftest" in argv:
        return selftest()
    if "--check-update" in argv:  # python3 app.py --check-update (no window)
        try:
            info = updater.fetch_latest(VERSION)
        except Exception as e:  # noqa: BLE001
            print("Could not check:", e)
            return 2
        print(f"Current version: {VERSION} | latest published version: {info['tag']} | "
              + ("update available: " + info["download"] if info["newer"] else "up to date"))
        return 0
    if "--smoketest" in argv:
        shot = argv[argv.index("--screenshot") + 1] if "--screenshot" in argv else None
        return smoketest(shot)
    app = App()
    paths = [p for p in argv if p.lower().endswith(".obj") and os.path.isfile(p)]
    if paths:  # python3 app.py model.obj
        app.after(400, app.load, os.path.abspath(paths[0]))
    app.mainloop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
