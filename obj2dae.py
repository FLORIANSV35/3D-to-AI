"""OBJ -> DAE (Collada 1.4.1) conversion engine, no user interface.

Designed for meshes from drone photogrammetry : big files,
arbitrary units, variable vertical axis. Textures are ignored
(only the Kd diffuse colour of the materials is kept).
"""
import os
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

import numpy as np

UNITS_TO_METERS = {
    "meters (m)": 1.0,
    "centimeters (cm)": 0.01,
    "millimeters (mm)": 0.001,
    "feet (ft)": 0.3048,
    "inches (in)": 0.0254,
}


@dataclass
class Mesh:
    verts: np.ndarray  # (N, 3) float64
    tris: np.ndarray  # (M, 3) int64
    tri_mat: np.ndarray  # (M,) int32, index into mat_names, -1 = none
    mat_names: list
    mat_colors: dict  # nom -> (r, g, b)
    vts: object = None  # (K, 2) texture coordinates, or None
    tri_vt: object = None  # (M, 3) indices into vts, or None
    mat_tex: dict = None  # name -> image path


def _noop(*_):
    pass


def parse_mtl(path):
    """Returns (Kd colours, map_Kd images) by material name."""
    colors, textures = {}, {}
    if not os.path.isfile(path):
        return colors, textures
    cur = None
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            p = line.split()
            if not p:
                continue
            if p[0] == "newmtl" and len(p) > 1:
                cur = " ".join(p[1:])
            elif p[0] == "Kd" and cur and len(p) >= 4:
                try:
                    colors[cur] = tuple(float(x) for x in p[1:4])
                except ValueError:
                    pass
            elif p[0] == "map_Kd" and cur and len(p) > 1:
                rest = re.sub(r"^(?:-\w+(?:\s+-?[\d.]+)+\s+)+", "", line.split(None, 1)[1].strip())
                name = rest
                img = os.path.join(os.path.dirname(path), name.replace("\\", "/"))
                textures[cur] = img
    return colors, textures


def load_obj(path, progress=_noop):
    """Read an .obj (v, f, usemtl, mtllib). Polygons are fan-triangulated."""
    size = max(os.path.getsize(path), 1)
    verts = []
    vts = []
    tris = []
    tri_vt = []
    tri_mat = []
    mat_index = {}
    mat_names = []
    cur_mat = -1
    mtl_files = []
    read = 0
    n = 0

    with open(path, "r", errors="replace") as fh:
        for line in fh:
            read += len(line)
            n += 1
            if n % 200000 == 0:
                progress(read / size * 0.8, "Reading the OBJ…")
            if line.startswith("v "):
                verts.append(line.split()[1:4])
            elif line.startswith("vt "):
                vts.append(line.split()[1:3])
            elif line.startswith("f "):
                nv, nt = len(verts), len(vts)
                idx, tex = [], []
                for tok in line.split()[1:]:
                    f = tok.split("/")
                    i = int(f[0])
                    idx.append(i - 1 if i > 0 else nv + i)
                    if len(f) > 1 and f[1]:
                        j = int(f[1])
                        tex.append(j - 1 if j > 0 else nt + j)
                    else:
                        tex.append(-1)
                a = idx[0]
                for k in range(1, len(idx) - 1):
                    tris.append((a, idx[k], idx[k + 1]))
                    tri_vt.append((tex[0], tex[k], tex[k + 1]))
                    tri_mat.append(cur_mat)
            elif line.startswith("usemtl"):
                name = line[6:].strip()
                if name not in mat_index:
                    mat_index[name] = len(mat_names)
                    mat_names.append(name)
                cur_mat = mat_index[name]
            elif line.startswith("mtllib"):
                mtl_files.append(line[6:].strip())

    if not verts or not tris:
        raise ValueError("No geometry (v / f) found in this OBJ file.")

    v = np.array(verts, dtype=np.float64)
    t = np.array(tris, dtype=np.int64)
    if t.min() < 0 or t.max() >= len(v):
        raise ValueError("Invalid face indices in the OBJ.")

    colors, textures = {}, {}
    base = os.path.dirname(path)
    for m in mtl_files:
        c, tx = parse_mtl(os.path.join(base, m))
        colors.update(c)
        textures.update(tx)

    uv = tv = None
    if vts:
        tv = np.array(tri_vt, dtype=np.int64)
        if tv.max() < len(vts) and tv.min() >= -1:
            uv = np.array(vts, dtype=np.float64)
        else:
            tv = None

    progress(0.8, "Mesh loaded")
    return Mesh(v, t, np.array(tri_mat, dtype=np.int32), mat_names, colors, uv, tv, textures)


def _cluster(mesh, cell):
    v = mesh.verts
    bmin = v.min(axis=0)
    keys = np.floor((v - bmin) / cell).astype(np.int64)
    dims = keys.max(axis=0) + 1
    flat = keys[:, 0] + dims[0] * (keys[:, 1] + dims[1] * keys[:, 2])
    _, inv = np.unique(flat, return_inverse=True)
    inv = inv.ravel()
    k = inv.max() + 1
    cnt = np.bincount(inv, minlength=k).astype(np.float64)
    nv = np.empty((k, 3))
    for a in range(3):
        nv[:, a] = np.bincount(inv, weights=v[:, a], minlength=k) / cnt
    t = inv[mesh.tris]
    ok = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    return Mesh(nv, t[ok], mesh.tri_mat[ok], mesh.mat_names, mesh.mat_colors)


def decimate(mesh, target_tris, progress=_noop):
    """Reduction by clustering vertices on a grid (binary search of the
    cell size to approach the wanted triangle count)."""
    if len(mesh.tris) <= target_tris:
        return mesh
    ext = (mesh.verts.max(axis=0) - mesh.verts.min(axis=0)).max()
    lo, hi = ext / 1e6, ext  # small cell = many triangles
    best = None
    for i in range(14):
        mid = (lo * hi) ** 0.5
        res = _cluster(mesh, mid)
        progress(0.8 + 0.1 * (i + 1) / 14, "Simplifying the mesh…")
        if len(res.tris) > target_tris:
            lo = mid
        else:
            hi = mid
            best = res
        if best is not None and len(best.tris) > target_tris * 0.9:
            break
    return best if best is not None else _cluster(mesh, hi)


def to_zup(v, src_up):
    """Convert points to a Z-up frame (Y-up : (x, y, z) -> (x, -z, y))."""
    if src_up == "Y":
        return np.stack([v[:, 0], -v[:, 2], v[:, 1]], axis=1)
    return v


def rotation_matrix(rx, ry, rz):
    """Rotations in degrees applied in the order X, then Y, then Z (Z-up frame)."""
    a, b, c = np.radians([rx, ry, rz])
    ca, sa, cb, sb, cc, sc = np.cos(a), np.sin(a), np.cos(b), np.sin(b), np.cos(c), np.sin(c)
    Rx = np.array([[1, 0, 0], [0, ca, -sa], [0, sa, ca]])
    Ry = np.array([[cb, 0, sb], [0, 1, 0], [-sb, 0, cb]])
    Rz = np.array([[cc, -sc, 0], [sc, cc, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def rotate(v, rot):
    """Apply rot=(rx, ry, rz) around the ORIGIN of the frame (the XYZ frame of AI) : the model turns like a
    solid around it. (To pivot around a point of the model, first put the origin on that point.)"""
    if not rot or not any(rot):
        return v
    return v @ rotation_matrix(*rot).T


def auto_level(zup, sample=200_000):
    """Angles (rx, ry) in degrees that level the ground.
    Plane fitted on the lowest 25 % of the points (the ground)."""
    if len(zup) > sample:
        zup = zup[np.random.default_rng(0).choice(len(zup), sample, replace=False)]
    low = zup[zup[:, 2] <= np.percentile(zup[:, 2], 25)]
    A = np.c_[low[:, 0], low[:, 1], np.ones(len(low))]
    (a, b, _), *_ = np.linalg.lstsq(A, low[:, 2], rcond=None)
    nx, ny, nz = np.array([-a, -b, 1.0]) / np.sqrt(a * a + b * b + 1)
    h = np.hypot(ny, nz)
    return float(np.degrees(np.arctan2(ny, nz))), float(-np.degrees(np.arctan2(nx, h)))


def auto_yaw(zup_leveled, sample=200_000):
    """Angle rz (degrees) that aligns the main horizontal axis of the model with X."""
    if len(zup_leveled) > sample:
        zup_leveled = zup_leveled[np.random.default_rng(0).choice(len(zup_leveled), sample, replace=False)]
    c = np.cov(zup_leveled[:, :2].T)
    theta = 0.5 * np.arctan2(2 * c[0, 1], c[0, 0] - c[1, 1])
    return float(-np.degrees(theta))


def orient(verts, src_up, rot=None, dst_up="Z"):
    """Vertical axis + rotation, without scale or centering."""
    v = rotate(to_zup(verts, src_up), rot)
    if dst_up == "Y":  # Z -> Y : (x, y, z) -> (x, z, -y)
        v = np.stack([v[:, 0], v[:, 2], -v[:, 1]], axis=1)
    return v


def euler_from_matrix(R):
    """Inverse of rotation_matrix : (rx, ry, rz) in degrees."""
    sb = -R[2, 0]
    b = np.arcsin(np.clip(sb, -1, 1))
    if abs(np.cos(b)) > 1e-8:
        a = np.arctan2(R[2, 1], R[2, 2])
        c = np.arctan2(R[1, 0], R[0, 0])
    else:  # gimbal lock
        a, c = 0.0, np.arctan2(-R[0, 1], R[1, 1])
    return tuple(float(np.degrees(x)) for x in (a, b, c))


def rotate_world(rot, axis, degrees):
    """Add a rotation around an axis of the frame (0=X, 1=Y, 2=Z) after the current rotation."""
    d = [0.0, 0.0, 0.0]
    d[axis] = degrees
    return euler_from_matrix(rotation_matrix(*d) @ rotation_matrix(*(rot or (0, 0, 0))))


def place(verts, scale_to_m, src_up, dst_up, rot=None, center=False, floor=False, offset=None):
    """Full chain : vertical axis, rotation, XY centering, ground at 0, scale, then offset (m)."""
    v = orient(verts, src_up, rot, dst_up)
    up = 2 if dst_up == "Z" else 1
    hor = [a for a in range(3) if a != up]
    if center:
        lo, hi = v.min(axis=0), v.max(axis=0)
        for a in hor:
            v[:, a] -= (lo[a] + hi[a]) / 2
    if floor:
        v[:, up] -= v[:, up].min()
    v = v * scale_to_m
    if offset is not None and any(offset):
        v = v + np.asarray(offset, dtype=float)
    return v


def transform(mesh, scale_to_m, src_up, dst_up, center, rot=None, floor=False, offset=None):
    v = place(mesh.verts, scale_to_m, src_up, dst_up, rot, center, floor, offset)
    return Mesh(v, mesh.tris, mesh.tri_mat, mesh.mat_names, mesh.mat_colors)


def sample_surface(verts, tris, n=250_000, seed=0, with_bary=False):
    """Points spread over the triangles (proportionally to the area) + unit normals.
    Used by the preview : a solid surface reads far better than the vertices alone."""
    p0, p1, p2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    cr = np.cross(p1 - p0, p2 - p0)
    area = np.linalg.norm(cr, axis=1)
    tot = area.sum()
    if tot <= 0:
        return verts, np.tile([0.0, 0.0, 1.0], (len(verts), 1))  # (no colours in this degenerate case)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(tris), size=n, p=area / tot)
    r1, r2 = np.sqrt(rng.random(n))[:, None], rng.random(n)[:, None]
    pts = (1 - r1) * p0[idx] + r1 * (1 - r2) * p1[idx] + r1 * r2 * p2[idx]
    nrm = cr[idx] / np.maximum(area[idx], 1e-300)[:, None]
    if with_bary:
        return pts, nrm, idx, np.hstack([1 - r1, r1 * (1 - r2), r1 * r2])
    return pts, nrm


_TEX_CACHE = {}


def load_texture(path, max_side=4096):
    """Image -> (H, W, 3) uint8 array, downsized if huge. None if missing/unreadable."""
    if path in _TEX_CACHE:
        return _TEX_CACHE[path]
    arr = None
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            im.draft("RGB", (max_side, max_side))
            im = im.convert("RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
            arr = np.asarray(im)
    except Exception:  # noqa: BLE001  (PIL missing, file missing or unreadable)
        arr = None
    _TEX_CACHE[path] = arr
    return arr


def surface_colors(mesh, idx, bary):
    """Colour (0-255) of each sampled point : texture if possible, otherwise Kd, otherwise NaN.
    Returns (colours (n,3) float, number of textures used)."""
    n = len(idx)
    out = np.full((n, 3), np.nan)
    used = 0
    tm = mesh.tri_mat[idx]
    for m, name in enumerate(mesh.mat_names):
        sel = np.nonzero(tm == m)[0]
        if not len(sel):
            continue
        tex = None
        if mesh.mat_tex and name in mesh.mat_tex and mesh.vts is not None:
            tex = load_texture(mesh.mat_tex[name])
        if tex is not None:
            vt = mesh.tri_vt[idx[sel]]  # (k, 3)
            ok = (vt >= 0).all(axis=1)
            uv = (mesh.vts[np.maximum(vt, 0)] * bary[sel][:, :, None]).sum(axis=1)
            h, w = tex.shape[:2]
            x = np.clip((uv[:, 0] % 1.0) * (w - 1), 0, w - 1).astype(int)
            y = np.clip((1 - (uv[:, 1] % 1.0)) * (h - 1), 0, h - 1).astype(int)
            col = tex[y, x].astype(float)
            out[sel[ok]] = col[ok]
            used += 1
        elif name in mesh.mat_colors:
            out[sel] = np.array(mesh.mat_colors[name]) * 255
    return out, used


def guess_up_axis(verts):
    """Photogrammetry : the vertical axis is almost always the one with the smallest extent (Y or Z)."""
    ext = np.ptp(verts, axis=0)
    return "Z" if ext[2] <= ext[1] else "Y"


def points_in_polygon(xy, poly):
    """Vectorised even-odd test : xy (N,2), poly (E,2) -> boolean mask (N,)."""
    poly = np.asarray(poly, dtype=float)
    x, y = xy[:, 0], xy[:, 1]
    inside = np.zeros(len(xy), dtype=bool)
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi, xj, yj = poly[i, 0], poly[i, 1], poly[j, 0], poly[j, 1]
        if yi != yj:
            cross = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
            inside ^= cross
        j = i
    return inside


def crop_mesh(mesh, lo, hi, polygon=None, hor=(0, 1)):
    """Keep the triangles whose centre is inside the box [lo, hi] (output frame, meters) and,
    if polygon is given, inside that polygon (coordinates of the horizontal axes `hor`).
    Vertices that became useless are removed ; UVs/textures are not kept."""
    lo, hi = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
    cen = mesh.verts[mesh.tris].mean(axis=1)
    keep = np.all((cen >= lo) & (cen <= hi), axis=1)
    if polygon is not None and len(polygon) >= 3:
        keep &= points_in_polygon(cen[:, list(hor)], polygon)
    if not keep.any():
        raise ValueError("The crop area contains no triangle.")
    used, inv = np.unique(mesh.tris[keep], return_inverse=True)
    return Mesh(mesh.verts[used], inv.reshape(-1, 3), mesh.tri_mat[keep], mesh.mat_names, mesh.mat_colors)


def vertex_normals(verts, tris):
    p0, p1, p2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    fn = np.cross(p1 - p0, p2 - p0)  # area-weighted
    n = np.zeros_like(verts)
    for k in range(3):
        for a in range(3):
            n[:, a] += np.bincount(tris[:, k], weights=fn[:, a], minlength=len(verts))
    ln = np.linalg.norm(n, axis=1)
    ln[ln == 0] = 1.0
    return n / ln[:, None]


def _floats(arr):
    return " ".join(map("{:.6g}".format, arr.ravel().tolist()))


def _sid(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "mat"


def write_dae(mesh, path, up_axis="Z", with_normals=True, single_group=True, progress=_noop):
    """single_group : all the triangles in ONE SINGLE <triangles>. The AI importer overwrites the
    indices at every group it meets (it only keeps the last one) : mandatory for AI."""
    verts, tris = mesh.verts, mesh.tris
    out = []
    w = out.append
    w('<?xml version="1.0" encoding="utf-8"?>\n')
    w('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n')
    w("  <asset>\n    <contributor><authoring_tool>3D to AI obj2dae</authoring_tool></contributor>\n")
    w('    <unit name="meter" meter="1"/>\n')
    w(f"    <up_axis>{up_axis}_UP</up_axis>\n  </asset>\n")

    # triangle groups : (material, triangles)
    used = sorted(set(mesh.tri_mat.tolist()))
    if single_group:
        used = used[:1]
        groups = [(used[0], tris)]
    else:
        groups = [(i, tris[mesh.tri_mat == i]) for i in used]
    names = {i: (_sid(mesh.mat_names[i]) if i >= 0 else "default") for i in used}
    colors = {}
    for i in used:
        c = mesh.mat_colors.get(mesh.mat_names[i]) if i >= 0 else None
        colors[i] = c or (0.7, 0.7, 0.7)

    w("  <library_effects>\n")
    for i in used:
        r, g, b = colors[i]
        w(f'    <effect id="{names[i]}-fx"><profile_COMMON><technique sid="common"><phong>'
          f'<diffuse><color>{r:.4f} {g:.4f} {b:.4f} 1</color></diffuse>'
          f"</phong></technique></profile_COMMON></effect>\n")
    w("  </library_effects>\n  <library_materials>\n")
    for i in used:
        w(f'    <material id="{names[i]}" name="{escape(names[i])}"><instance_effect url="#{names[i]}-fx"/></material>\n')
    w("  </library_materials>\n")

    progress(0.92, "Writing the DAE…")
    nv = len(verts)
    w('  <library_geometries>\n    <geometry id="mesh-geometry" name="mesh"><mesh>\n')
    w(f'      <source id="mesh-positions"><float_array id="mesh-positions-array" count="{nv * 3}">'
      f"{_floats(verts)}</float_array><technique_common>"
      f'<accessor source="#mesh-positions-array" count="{nv}" stride="3">'
      '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
      "</accessor></technique_common></source>\n")
    if with_normals:
        nrm = vertex_normals(verts, tris)
        w(f'      <source id="mesh-normals"><float_array id="mesh-normals-array" count="{nv * 3}">'
          f"{_floats(nrm)}</float_array><technique_common>"
          f'<accessor source="#mesh-normals-array" count="{nv}" stride="3">'
          '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
          "</accessor></technique_common></source>\n")
    # like SketchUp : POSITION first (AI only reads the 1st input), normals in <vertices>,
    # and a SINGLE VERTEX input in <triangles> (AI reads one index per <input> tag).
    w('      <vertices id="mesh-vertices"><input semantic="POSITION" source="#mesh-positions"/>')
    if with_normals:
        w('<input semantic="NORMAL" source="#mesh-normals"/>')
    w("</vertices>\n")
    for i, sel in groups:
        w(f'      <triangles material="{names[i]}" count="{len(sel)}">'
          '<input semantic="VERTEX" source="#mesh-vertices" offset="0"/>')
        w(f"<p>{' '.join(map(str, sel.ravel().tolist()))}</p></triangles>\n")
    w("    </mesh></geometry>\n  </library_geometries>\n")

    w('  <library_visual_scenes>\n    <visual_scene id="Scene" name="Scene">\n')
    w('      <node id="mesh-node" name="mesh" type="NODE"><instance_geometry url="#mesh-geometry">'
      "<bind_material><technique_common>")
    for i in used:
        w(f'<instance_material symbol="{names[i]}" target="#{names[i]}"/>')
    w("</technique_common></bind_material></instance_geometry></node>\n")
    w('    </visual_scene>\n  </library_visual_scenes>\n')
    w('  <scene><instance_visual_scene url="#Scene"/></scene>\n</COLLADA>\n')

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
    progress(1.0, "Done")


def convert(src, dst, unit_factor=1.0, src_up="Y", dst_up="Z", center=False,
            target_tris=None, with_normals=True, rot=None, floor=False, offset=None, single_group=True,
            crop=None, progress=_noop, mesh=None):
    """mesh : mesh already in memory (topographic survey) ; otherwise src is read.
    crop : (lo, hi) or (lo, hi, polygon) : box (x, y, z) in meters in the output frame (after
    placement) ; the optional polygon is a list of points (h1, h2) on the horizontal output axes
    (X, Y if the output is Z-up ; X, Z if it is Y-up)."""
    mesh = mesh if mesh is not None else load_obj(src, progress)
    stats = {"tris_in": len(mesh.tris), "verts_in": len(mesh.verts)}
    mesh = transform(mesh, unit_factor, src_up, dst_up, center, rot, floor, offset)
    if crop is not None:
        lo, hi, *poly = crop
        mesh = crop_mesh(mesh, lo, hi, poly[0] if poly else None, (0, 1) if dst_up == "Z" else (0, 2))
    stats["tris_cropped"] = len(mesh.tris)
    if target_tris:
        mesh = decimate(mesh, target_tris, progress)
    write_dae(mesh, dst, dst_up, with_normals, single_group, progress)
    stats.update(tris_out=len(mesh.tris), verts_out=len(mesh.verts),
                 size_m=(mesh.verts.max(axis=0) - mesh.verts.min(axis=0)).tolist())
    return stats


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Convert an .obj to .dae (Collada)")
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--scale", type=float, default=1.0, help="factor to meters")
    ap.add_argument("--src-up", choices="YZ", default="Y")
    ap.add_argument("--dst-up", choices="YZ", default="Z")
    ap.add_argument("--center", action="store_true", help="center the model in X/Y")
    ap.add_argument("--floor", action="store_true", help="put the lowest point at 0")
    ap.add_argument("--offset", type=float, nargs=3, metavar=("DX", "DY", "DZ"), help="final offset in meters")
    ap.add_argument("--max-tris", type=int)
    ap.add_argument("--polygon", type=float, nargs="+", metavar="X Y",
                    help="crop polygon seen from above : x1 y1 x2 y2 x3 y3 … (meters, output frame)")
    ap.add_argument("--crop", type=float, nargs=6, metavar=("XMIN", "XMAX", "YMIN", "YMAX", "ZMIN", "ZMAX"),
                    help="keep the area (meters, output frame)")
    ap.add_argument("--multi-group", action="store_true", help="one <triangles> per material (NOT AI-compatible)")
    ap.add_argument("--rot", type=float, nargs=3, metavar=("RX", "RY", "RZ"), help="degrees, order X,Y,Z")
    ap.add_argument("--auto-level", action="store_true", help="level the ground")
    ap.add_argument("--auto-yaw", action="store_true", help="align the main axis with X")
    a = ap.parse_args()
    dst = a.dst or os.path.splitext(a.src)[0] + ".dae"
    rot = tuple(a.rot) if a.rot else (0.0, 0.0, 0.0)
    if a.auto_level or a.auto_yaw:
        z = to_zup(load_obj(a.src).verts, a.src_up)
        if a.auto_level:
            rot = (*auto_level(z), rot[2])
        if a.auto_yaw:
            rot = (rot[0], rot[1], auto_yaw(rotate(z, rot)))
        print("rotation applied (deg):", rot)
    cropbox = None
    if a.crop:
        cropbox = ((a.crop[0], a.crop[2], a.crop[4]), (a.crop[1], a.crop[3], a.crop[5]))
    if a.polygon:
        pts = list(zip(a.polygon[0::2], a.polygon[1::2]))
        lo, hi = cropbox or ((-1e9,) * 3, (1e9,) * 3)
        cropbox = (lo, hi, pts)
    print(convert(a.src, dst, a.scale, a.src_up, a.dst_up, a.center, a.max_tris, rot=rot,
                  floor=a.floor, offset=a.offset, single_group=not a.multi_group,
                  crop=cropbox))
