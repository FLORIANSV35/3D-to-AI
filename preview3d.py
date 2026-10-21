"""3D preview (point cloud sampled on the surface), numpy rendering -> PhotoImage (PPM).

Optimizations : 16-bit depth sort (radix sort), precomputed colours, pixels packed as
uint32 (one write per point), reduced level of detail during gestures then a sharp image at rest,
and merged mouse events (a single render per frame).
"""
import tkinter as tk

import numpy as np

import obj2dae
import theme

LOD_POINTS = 70_000  # number of points shown during a gesture (rotation, zoom, pan)
SETTLE_MS = 160  # delay without gesture before the full render


def _pk(rgb):
    """Colour (r, g, b) -> uint32 pixel (bytes R, G, B, A in little-endian memory)."""
    r, g, b = rgb
    return np.uint32(r | (g << 8) | (b << 16) | (255 << 24))


_SPRITES = {}


def _sprite(text, rgb, size):
    """Small text sprite (dark outline to stay readable on any background) :
    (rgb float32 (h,w,3), alpha float32 (h,w,1)), or None if Pillow is missing."""
    key = (text, rgb, size)
    if key not in _SPRITES:
        try:
            from PIL import Image, ImageDraw, ImageFont
            try:
                font = ImageFont.load_default(size)  # Pillow >= 10.1 : vector font at the wanted size
            except TypeError:
                font = ImageFont.load_default()
            pad = 3
            probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
            x0, y0, x1, y1 = probe.textbbox((0, 0), text, font=font, stroke_width=1)
            im = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((pad - x0, pad - y0), text, font=font, fill=tuple(rgb) + (255,),
                                    stroke_width=1, stroke_fill=(18, 20, 18, 255))
            arr = np.asarray(im, dtype=np.float32)
            _SPRITES[key] = (arr[:, :, :3], arr[:, :, 3:] / 255.0)
        except Exception:  # noqa: BLE001  (Pillow missing or font unavailable : no label)
            _SPRITES[key] = None
    return _SPRITES[key]


class Preview(tk.Label):
    def __init__(self, master, on_moved=None, on_rotated=None, on_status=None):
        super().__init__(master, bg=theme.VIEWPORT, cursor="fleur", bd=0, highlightthickness=0)
        self.on_moved = on_moved  # end of dragging the frame : delta (m, Z-up frame)
        self.on_rotated = on_rotated  # end of a rotation on a ring / by button : (axis 0-2, degrees)
        self.on_status = on_status  # status text during the rotation (angle)
        self._ring_hot = None  # ring hovered or grabbed : 'X' / 'Y' / 'Z'
        self._anim = False
        # View anchored on the MODEL : the entered rotation does not move the model on screen, the XYZ frame
        # (and the crop zone) rotates instead. frame_pts applies to the points, frame_ov to the frame and overlays ;
        # they are equal at rest and only differ during a rotation gesture.
        self.frame_pts = np.eye(3)
        self.frame_ov = np.eye(3)
        self.ov_shift = np.zeros(3)  # translation of the frame during a gesture : it pivots around ITS origin
        self.unit_m = 1.0  # meters per normalized unit
        self._grab = None
        self.pick_cb = None  # if set : the next click picks a point of the model
        self.crop = None
        self.outside = None
        self.zup = None
        self.c = np.zeros(3)
        self.pan = [0.0, 0.0]  # view offset (normalized units)
        self.poly = None  # finished crop polygon : points (X, Y) in Z-up frame
        self.poly_mode = False
        self.draft = []  # polygon being drawn
        self.on_poly_done = None
        self.on_poly_status = None
        self.pts = None  # (N,3) normalized float64, display frame (Y up)
        self.pts32 = None
        self.base = None  # (N,3) float32 : colour × lighting, without the depth effect
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
        # right click (Button-2 on macOS, Button-3 elsewhere) or middle button + drag : pan the view
        for b in (2, 3):
            self.bind(f"<ButtonPress-{b}>", self._pan_press)
            self.bind(f"<B{b}-Motion>", self._pan_drag)
            self.bind(f"<ButtonRelease-{b}>", self._pan_release)
        self.bind("<Double-Button-1>", lambda e: None if self.poly_mode else self.reset())

    # ------------------------------------------------------------------ data
    def set_points(self, zup, normals=None, colors=None, origin=(0.0, 0.0, 0.0), keep_view=False,
                   crop=None, poly=None, max_points=250_000, frame=None):
        """zup : (N,3) in Z-up frame, already placed. origin : where the output (0,0,0) is."""
        if len(zup) > max_points:
            keep = np.random.default_rng(0).choice(len(zup), max_points, replace=False)
            zup = zup[keep]
            normals = normals[keep] if normals is not None else None
            colors = colors[keep] if colors is not None else None
        self.zup = zup
        self.crop = crop  # (lo, hi) in Z-up frame, or None
        self.poly = poly
        inside = None if crop is None else np.all((zup >= crop[0]) & (zup <= crop[1]), axis=1)
        if poly is not None and len(poly) >= 3 and inside is not None:
            inside &= obj2dae.points_in_polygon(zup[:, :2], poly)
        self.outside = None if inside is None else ~inside

        disp = lambda q: np.stack([q[..., 0], q[..., 2], -q[..., 1]], axis=-1)  # display, Y up
        # frame : rotation matrix of the model (output = R · source). Centre and scale are taken in the frame of the
        # model, so that the model does not move on screen when the rotation changes.
        R = np.eye(3) if frame is None else np.asarray(frame, dtype=float)
        zm = zup @ R
        cm = (zm.max(axis=0) + zm.min(axis=0)) / 2
        c = R @ cm
        p = disp(zup - c)
        k = max(np.abs(zm - cm).max(), 1e-12)
        D = np.array([[1.0, 0, 0], [0, 0, 1], [0, -1, 0]])
        self.frame_pts = self.frame_ov = D @ R.T @ D.T  # output frame -> model frame (display)
        self.ov_shift = np.zeros(3)
        self.pts = p / k
        self.pts32 = np.ascontiguousarray(self.pts, dtype=np.float32)
        self.origin = disp(np.asarray(origin, dtype=float) - c) / k
        self.unit_m = k
        self.c = c

        # base colour : height gradient or texture/material colour, × lighting, × greyed outside the zone
        t = np.clip((self.pts[:, 1] + 1) / 2, 0, 1)[:, None]  # height 0 (bottom) -> 1 (top)
        stops = np.array(theme.HEIGHT_RAMP, dtype=float)  # 3-colour gradient
        col = np.where(t < 0.5, stops[0] + (stops[1] - stops[0]) * (t * 2), stops[1] + (stops[2] - stops[1]) * (t * 2 - 1))
        lit = np.full(len(zup), 0.8) if normals is None else 0.3 + 0.7 * np.abs(normals[:, 2])
        if colors is not None:  # real colours / textures, softer lighting to preserve them
            has = ~np.isnan(colors[:, 0])
            col[has] = colors[has]
            lit = np.where(has, 0.7 + 0.3 * lit, lit)
        if self.outside is not None:
            lit = np.where(self.outside, lit * 0.2, lit)
        self.base = (col * lit[:, None]).astype(np.float32)

        self.render() if keep_view else self.reset()

    # ------------------------------------------------------------------ views
    def reset(self):
        self.set_view("iso")

    def set_view(self, name):
        self.yaw, self.pitch = {"iso": (0.6, 0.45), "top": (0.0, self.TOP_PITCH),
                                "front": (0.0, 0.0), "side": (float(np.pi / 2), 0.0)}[name]
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        self.render()

    def _cam(self):
        """Camera rotation (rows : screen x, screen y, depth), as float64."""
        cy, sy, cp, sp = np.cos(self.yaw), np.sin(self.yaw), np.cos(self.pitch), np.sin(self.pitch)
        return np.array([[cy, 0, sy],
                         [sp * sy, cp, -sp * cy],
                         [-cp * sy, sp, cp * cy]])

    def _rot(self, pts=False):
        """Current rotation as float32 : camera × model frame."""
        return (self._cam() @ (self.frame_pts if pts else self.frame_ov)).astype(np.float32)

    def _project(self, P, w, h):
        """P (N,3) normalized -> (px, py, depth) as floats (larger depth = closer)."""
        r = self._rot().astype(np.float64)
        off = self._cam() @ self.ov_shift if np.any(self.ov_shift) else 0.0
        x1, y2, z2 = (np.asarray(P, dtype=np.float64) @ r.T + off).T
        s = min(w, h) * 0.45 * self.zoom
        return w / 2 + (x1 + self.pan[0]) * s, h / 2 - (y2 + self.pan[1]) * s, z2

    # ------------------------------------------------------------------ rendering
    def request_render(self):
        """Light render (gesture in progress) at the next frame, then full render at rest."""
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
        """Maximum depth of the model in a (2r+1)² square around each point (gx, gy)."""
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
            P = self.pts32[:n] @ self._rot(True).T  # screen x, screen y, depth
            s = np.float32(min(w, h) * 0.45 * self.zoom)
            px = (w / 2 + (P[:, 0] + self.pan[0]) * s).astype(np.int32)
            py = (h / 2 - (P[:, 1] + self.pan[1]) * s).astype(np.int32)
            ok = np.nonzero((px >= 1) & (px < w - 2) & (py >= 1) & (py < h - 2))[0]
            z = P[ok, 2]
            # 16-bit depth sort (radix) : far first, the closest overwrites last
            zq = ((z + 1.8) * np.float32(65535 / 3.6)).clip(0, 65535).astype(np.uint16)
            order = np.argsort(zq, kind="stable")
            idx, z = ok[order], z[order]
            lin = py[idx] * w + px[idx]
            shade = np.clip(0.85 + 0.15 * z, 0.5, 1.15)[:, None]
            rgba = np.empty((len(idx), 4), dtype=np.uint8)
            rgba[:, :3] = np.minimum(self.base[idx] * shade, 255)
            rgba[:, 3] = 255
            packed = rgba.view(np.uint32).ravel()
            zb = np.full(h * w, -np.inf, dtype=np.float32)  # depth of the model (larger = closer)
            for off in ((0, 1, w, w + 1) if f == 1 else (0,)):  # 2 px points on screen
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
        """XYZ frame (hidden by the model), crop box/polygon, drawing in progress."""
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

        # output frame (0,0,0) : X red, Y green, Z blue, hidden where the model passes in front
        if self.show_axes and self.origin is not None:
            ramp = np.linspace(0, self.GIZMO_LEN, 80)[:, None]
            rgba = img.view(np.uint8).reshape(h, w, 4)  # same memory as img, to blend the labels
            for name, d, colr in (("X", (1, 0, 0), theme.AXIS_X), ("Y", (0, 0, -1), theme.AXIS_Y),
                                  ("Z", (0, 1, 0), theme.AXIS_Z)):
                gx, gy, gd = self._project(self.origin + ramp * np.array(d, dtype=float), w, h)
                gx, gy = gx.astype(int), gy.astype(int)
                ok = (gx >= r + 2) & (gx < w - r - 2) & (gy >= r + 2) & (gy < h - r - 2)
                tip_ok = bool(ok[-1])
                tx, ty = int(gx[-1]), int(gy[-1])
                ox, oy = int(gx[0]), int(gy[0])
                gx, gy, gd = gx[ok], gy[ok], gd[ok]
                vis = gd >= self._local_max(zb, w, gx, gy, r) - 0.02  # tolerance : an axis lying on the ground stays visible
                paint(gx[vis], gy[vis], colr, thick)
                # label at the end of the axis, offset along the axis ; hidden if the tip is
                label = theme.AXIS_LABELS.get(name)
                if label and tip_ok and vis.size and vis[-1]:
                    spr = _sprite(label, colr, 13 if f == 1 else 8)
                    if spr is not None:
                        rgb, alpha = spr
                        sh, sw = alpha.shape[:2]
                        v = np.array([tx - ox, ty - oy], dtype=float)
                        v = v / max(np.hypot(*v), 1e-6)
                        gap = 0.5 * sw * abs(v[0]) + 0.5 * sh * abs(v[1]) + 4
                        x0, y0 = int(tx + v[0] * gap - sw / 2), int(ty + v[1] * gap - sh / 2)
                        cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x0 + sw, w), min(y0 + sh, h)
                        if cx1 > cx0 and cy1 > cy0:
                            sx0, sy0 = cx0 - x0, cy0 - y0
                            a_ = alpha[sy0:sy0 + cy1 - cy0, sx0:sx0 + cx1 - cx0]
                            c_ = rgb[sy0:sy0 + cy1 - cy0, sx0:sx0 + cx1 - cx0]
                            reg = rgba[cy0:cy1, cx0:cx1, :3]
                            reg[:] = (reg * (1 - a_) + c_ * a_).astype(np.uint8)

        # rotation rings around each axis : grab one to turn the model around that axis
        if self.show_axes and self.origin is not None:
            od = float(self._project(self.origin[None, :], w, h)[2][0])
            for name, colr in (("X", theme.AXIS_X), ("Y", theme.AXIS_Y), ("Z", theme.AXIS_Z)):
                hot = name == self._ring_hot
                sx, sy, sd = self._project(self._ring(name, 480)[0], w, h)
                sx, sy = sx.astype(int), sy.astype(int)
                ok = (sx >= 2) & (sx < w - 2) & (sy >= 2) & (sy < h - 2)
                front = sd >= od
                bright = tuple(int(c + (255 - c) * 0.55) for c in colr) if hot else colr
                dim = tuple(int(c * (0.7 if hot else 0.45)) for c in colr)
                paint(sx[ok & ~front], sy[ok & ~front], dim, thick if hot else (0,))
                paint(sx[ok & front], sy[ok & front], bright, thick)

        vw = lambda x, y, z: np.array([x - self.c[0], z - self.c[2], -(y - self.c[1])]) / self.unit_m
        if self.poly_mode and self.draft:  # drawing in progress
            zc = self.c[2]
            for a_, b_ in zip(self.draft, self.draft[1:]):
                seg(vw(*a_, zc), vw(*b_, zc), theme.CROP, (0, 1) if f == 1 else (0,))
            for i, (x_, y_) in enumerate(self.draft):
                vx, vy, _ = self._project(vw(x_, y_, zc)[None, :], w, h)
                vx, vy, rr = int(vx[0]), int(vy[0]), 3 if f == 1 else 2
                if rr < vx < w - rr and rr < vy < h - rr:
                    img[vy - rr:vy + rr + 1, vx - rr:vx + rr + 1] = _pk(theme.CLOSE if (i == 0 and len(self.draft) >= 3) else theme.CROP)
        elif self.poly is not None and self.crop is not None and len(self.poly) >= 3:  # finished polygon : prism
            z0, z1 = self.crop[0][2], self.crop[1][2]
            for ring in (z0, z1):
                for a_, b_ in zip(self.poly, self.poly[1:] + self.poly[:1]):
                    seg(vw(*a_, ring), vw(*b_, ring), theme.CROP)
            for x_, y_ in self.poly:
                seg(vw(x_, y_, z0), vw(x_, y_, z1), theme.CROP_DIM)
        elif self.crop is not None:  # crop box
            lo, hi = (np.asarray(v, dtype=float) for v in self.crop)
            cn = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            cv = (np.stack([cn[:, 0], cn[:, 2], -cn[:, 1]], axis=1) - np.array([self.c[0], self.c[2], -self.c[1]])) / self.unit_m
            for i in range(8):
                for j in range(i + 1, 8):
                    if bin(i ^ j).count("1") == 1:  # an edge = two corners that differ along a single axis
                        seg(cv[i], cv[j], theme.CROP)

    # ------------------------------------------------------------------ XYZ frame (handle)
    GIZMO_LEN = 0.3
    # axes in display frame (Y up) : X, Y (depth inverted), Z (up)
    AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 0.0, -1.0), "Z": (0.0, 1.0, 0.0)}

    def _size(self):
        return max(self.winfo_width(), 50), max(self.winfo_height(), 50)

    def _sdir(self, d):
        """Screen vector (px) of a displacement of one normalized unit along d."""
        w, h = self._size()
        px, py, _ = self._project(np.array([[0.0, 0, 0], d]), w, h)
        return np.array([px[1] - px[0], py[1] - py[0]])

    RING_R = 0.2  # radius of the rotation rings (normalized units)

    def _ring(self, name, n=120):
        """Points of the ring around the axis `name` + (axis, u, v) : right-handed basis (u × v = axis) of the ring plane."""
        a = np.array(self.AXES[name], dtype=float)
        h = np.array([0.0, 1.0, 0.0]) if abs(a[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
        u = np.cross(a, h)
        u /= np.linalg.norm(u)
        v = np.cross(a, u)
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)[:, None]
        return self.origin + self.RING_R * (np.cos(t) * u + np.sin(t) * v), (a, u, v)

    def _hit(self, x, y):
        """'C' (centre), 'X'/'Y'/'Z' (arrow), 'RX'/'RY'/'RZ' (rotation ring) or None."""
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
        best, hit = 8.0, None  # rotation ring closest to the cursor
        for name in self.AXES:
            sx, sy, _ = self._project(self._ring(name)[0], w, h)
            d = np.hypot(sx - x, sy - y).min()
            if d < best:
                best, hit = d, "R" + name
        return hit

    def _hover(self, e):
        if self.pick_cb or self.poly_mode:
            return
        hit = self._hit(e.x, e.y)
        hot = hit[1] if hit and hit[0] == "R" else None
        if hot != self._ring_hot:
            self._ring_hot = hot
            self.request_render()
        self.configure(cursor="exchange" if hot else ("hand2" if hit else "fleur"))

    # --- picking a point of the model -------------------------------------------
    def start_pick(self, cb):
        self.pick_cb = cb
        self.configure(cursor="crosshair")

    def cancel_pick(self):
        self.pick_cb = None
        self.configure(cursor="fleur")

    def _pick(self, x, y):
        """Point of the model under the cursor (closest to the screen within a few pixels)."""
        w, h = self._size()
        px, py, z2 = self._project(self.pts, w, h)
        for r in (6, 14, 30):
            near = np.nonzero((np.abs(px - x) <= r) & (np.abs(py - y) <= r))[0]
            if len(near):
                return self.zup[near[np.argmax(z2[near])]]
        return None

    # --- drawing a crop polygon (top view) -----------------------
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
                self.on_poly_status("At least 3 points are needed to close the polygon.")
            return
        pts, cb = list(self.draft), self.on_poly_done
        self._poly_end("")
        if cb:
            cb(pts)

    def _unproject(self, x, y):
        """Pixel -> (X, Y) in meters on the horizontal plane (output Z = centre of the model) of the output frame."""
        W, H = self._size()
        s = min(W, H) * 0.45 * self.zoom
        x1, y_ = (x - W / 2) / s - self.pan[0], (H / 2 - y) / s - self.pan[1]
        t = self._rot().astype(float)
        if abs(t[2, 1]) < 1e-3:  # plane seen edge-on : neither point nor polygon possible
            return (self.c[0], self.c[1])
        z = (0.0 - x1 * t[0, 1] - y_ * t[1, 1]) / t[2, 1]
        d = x1 * t[0] + y_ * t[1] + z * t[2]  # display point (Y up, centered)
        return (self.c[0] + d[0] * self.unit_m, self.c[1] - d[2] * self.unit_m)

    def _poly_click(self, x, y):
        if self.pts is None:
            return
        if len(self.draft) >= 3:  # click on the 1st point = close
            W, H = self._size()
            f0 = np.array([[self.draft[0][0] - self.c[0], 0.0, -(self.draft[0][1] - self.c[1])]]) / self.unit_m
            fx, fy, _ = self._project(f0, W, H)
            if np.hypot(fx[0] - x, fy[0] - y) <= 12:
                return self.finish_poly()
        self.draft.append(self._unproject(x, y))
        if self.on_poly_status:
            self.on_poly_status("%d point(s) — click the 1st point (green) or press Enter to close" % len(self.draft))
        self.render()

    # ------------------------------------------------------------------ mouse
    # --- panning the view ------------------------------------------------
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
        if e.state & 0x5:  # Shift or Ctrl + left click = pan the view (trackpad without right click)
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
        if self._anim:
            return
        hit = self._hit(e.x, e.y)
        self._grab = {"hit": hit, "start": (e.x, e.y), "o0": self.origin.copy()} if hit else None
        if hit and hit[0] == "R":
            self._begin_ring(hit[1], e.x, e.y)

    # --- rotation on a ring ---------------------------------------------------
    @staticmethod
    def _wrap(deg):
        return (deg + 180.0) % 360.0 - 180.0

    def _begin_ring(self, name, x, y):
        """Start of a drag on a ring : starting angle in the ring plane (or, if the ring is seen
        edge-on, movement along the ring tangent). Measurements use the frame at the start of the gesture."""
        g = self._grab
        g.update(axis=name, M0=self.frame_ov.copy(), deg=0.0, theta0=None)
        g["T0"] = self._rot().astype(float)
        self._ring_hot = name
        a = self.AXES[name]
        if abs(np.dot(a, g["T0"][2])) >= 0.2:
            g["theta0"] = self._ring_theta(name, x, y, g["T0"])
        else:  # edge-on ring : the plane does not project, follow the tangent on screen
            w, h = self._size()
            sx, sy, _ = self._project(self._ring(name, 360)[0], w, h)
            i = int(np.argmin(np.hypot(sx - x, sy - y)))
            tg = np.array([sx[(i + 1) % 360] - sx[i - 1], sy[(i + 1) % 360] - sy[i - 1]])
            g["tangent"] = tg / max(np.hypot(*tg), 1e-9)

    def _ring_theta(self, name, x, y, r):
        """Angle (degrees) of the screen point (x, y) projected on the ring plane, around the axis ; r = screen
        rotation (rows x, y, depth) to use."""
        W, H = self._size()
        s = min(W, H) * 0.45 * self.zoom
        x1, y2 = (x - W / 2) / s - self.pan[0], (H / 2 - y) / s - self.pan[1]
        _, (a, u, v) = self._ring(name, 4)
        z = (np.dot(a, self.origin) - np.dot(a, r[0]) * x1 - np.dot(a, r[1]) * y2) / np.dot(a, r[2])
        p = x1 * r[0] + y2 * r[1] + z * r[2] - self.origin
        return float(np.degrees(np.arctan2(np.dot(p, v), np.dot(p, u))))

    @staticmethod
    def _rodrigues(a, deg):
        t = np.radians(deg)
        K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
        return np.eye(3) + np.sin(t) * K + (1 - np.cos(t)) * (K @ K)

    def _turn_axes(self, name, deg, M0):
        """Rotate the XYZ frame (and the crop zone) by `deg` around the axis `name`, the model stays fixed."""
        rod = self._rodrigues(np.array(self.AXES[name], dtype=float), deg)
        self.frame_ov = M0 @ rod
        self.ov_shift = M0 @ (self.origin - rod @ self.origin)  # the pivot is the origin of the frame, not the centre of the model

    def _drag_ring(self, e):
        g = self._grab
        name = g["axis"]
        if g["theta0"] is not None:
            deg = self._wrap(self._ring_theta(name, e.x, e.y, g["T0"]) - g["theta0"])
            deg += 360.0 * round((g["deg"] - deg) / 360.0)  # continuity beyond ±180°
        else:
            w, h = self._size()
            rad = self.RING_R * min(w, h) * 0.45 * self.zoom
            dp = np.array([e.x - g["start"][0], e.y - g["start"][1]], dtype=float)
            deg = float(np.degrees(np.dot(dp, g["tangent"]) / max(rad, 1.0)))
        snap = round(deg / 15.0) * 15.0
        deg = snap if abs(deg - snap) < 2.0 else round(deg, 1)  # magnet on multiples of 15°
        g["deg"] = deg
        self._turn_axes(name, deg, g["M0"])
        if self.on_status:
            self.on_status("Axis %s turned by %+.1f°" % (name, deg))
        self.request_render()

    def animate_rotation(self, axis, deg, on_done):
        """Rotation of the MODEL by `deg` around axis 0/1/2 (X/Y/Z) of the frame, shown by rotating the XYZ frame
        on screen (the model stays fixed), then on_done(axis, degrees). Used by the ±90° buttons."""
        if self.pts is None or self.origin is None or self._anim or not self.winfo_ismapped():
            on_done(axis, deg)
            return
        name, M0, steps = "XYZ"[axis], self.frame_ov.copy(), 10
        self._anim, self._ring_hot, self._interactive = True, name, True

        def step(i):
            t = i / steps
            self._turn_axes(name, -deg * t * t * (3 - 2 * t), M0)  # the model turns by +deg : the frame by -deg
            self.render()
            if i < steps:
                self.after(16, lambda: step(i + 1))
            else:
                self._anim, self._ring_hot, self._interactive = False, None, False
                on_done(axis, deg)

        step(1)

    def _drag_gizmo(self, e):
        g = self._grab
        if g["hit"][0] == "R":
            return self._drag_ring(e)
        dp = np.array([e.x - g["start"][0], e.y - g["start"][1]], dtype=float)
        if g["hit"] == "C":  # horizontal plane : display directions X and -Y
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
        if g and g["hit"][0] == "R":
            self._ring_hot = None
            if self.on_status:
                self.on_status("")
            if abs(g["deg"]) > 1e-6 and self.on_rotated:
                self.on_rotated("XYZ".index(g["axis"]), -g["deg"])  # the frame turned by +deg : the model by -deg
            else:
                self.frame_ov, self.ov_shift = g["M0"], np.zeros(3)
                self.request_render()
            return
        if g and self.on_moved:
            dd = (self.origin - g["o0"]) * self.unit_m  # display (x, y up, z)
            if np.any(np.abs(dd) > 1e-9):
                self.on_moved((dd[0], -dd[2], dd[1]))  # -> Z-up frame

    def _drag(self, e):
        if self._pan_last is not None:
            return self._pan_drag(e)
        if self.poly_mode:  # in polygon mode, drag = pan the view (a simple click = add a point)
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
        """Zoom ×f ; if (x, y) is given, the model point under the cursor stays fixed on screen."""
        new = max(0.2, min(20, self.zoom * f))
        f = new / self.zoom
        if x is not None and f != 1:
            W, H = self._size()
            s = min(W, H) * 0.45 * self.zoom
            self.pan[0] += (x - W / 2) / s * (1 / f - 1)
            self.pan[1] += (H / 2 - y) / s * (1 / f - 1)
        self.zoom = new
        self.request_render()
