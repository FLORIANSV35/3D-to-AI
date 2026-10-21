"""Topographic survey from the internet -> 3D mesh (no user interface).

Sources (free, no key) :
  - IGN Géoplateforme (France) : RGE ALTI (DTM, bare ground, ~1 m) or DSM (surface : buildings, trees) ;
  - AWS Terrain Tiles (worldwide, SRTM/NASADEM/LiDAR depending on the country, 3 to 30 m) ;
  - OpenStreetMap Nominatim to find a place from an address.

The mesh produced is in meters, local Z-up frame, centered on the chosen place
(X = east, Y = north, Z = altitude).
"""
import io
import json
import math
import os
import re
import ssl
import tempfile
import time
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor

import numpy as np

import obj2dae

UA = "3D-to-AI/1.0 (OBJ/terrain -> DAE converter for Adamson ArrayIntelligence)"
IGN_WMS = "https://data.geopf.fr/wms-r/wms"
IGN_LAYERS = {
    "mnt": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES",
    "mns": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES.MNS",
}
AWS_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
NODATA = -99999.0
M_PER_DEG_LAT = 111132.0
MAX_GRID = 1500  # maximum points per side (IGN accepts 5000, but the mesh would become huge)
N_BANDS = 12  # coloured altitude bands (preview only)

TILE = 256
TILE_LAYERS = {  # name -> (tile URL template, source credit)
    "Satellite (Esri)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Imagery © Esri, Maxar, Earthstar Geographics"),
    "IGN orthophoto (France)": (
        "https://data.geopf.fr/wmts?SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile&LAYER=ORTHOIMAGERY.ORTHOPHOTOS"
        "&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg",
        "© IGN – Géoplateforme"),
    "Map (OpenStreetMap)": (
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "© OpenStreetMap contributors"),
}
TEXTURE_MAX_PX = 4096  # maximum side of the image draped over the terrain
SOURCES = ("Auto (IGN, then worldwide)", "IGN France (1 m)", "Worldwide (AWS, 3-30 m)")
KINDS = ("Bare terrain (DTM)", "Surface: buildings, trees (DSM, IGN)")


class TopoError(Exception):
    pass


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except Exception as e:  # noqa: BLE001
        raise TopoError(f"Connection failed ({e}). Check your internet connection.") from e


# --- lieu ---------------------------------------------------------------------------
_LATLON = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d+(?:[.,]\d+)?)\s*$")


def geocode(query):
    """Address / place name / 'lat, lon' -> (lat, lon, label)."""
    m = _LATLON.match(query)
    if m:
        lat, lon = (float(g.replace(",", ".")) for g in m.groups())
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise TopoError("Coordinates out of range (latitude, longitude).")
        return lat, lon, f"{lat:.5f}, {lon:.5f}"
    if not query.strip():
        raise TopoError("Enter a place (address, city…) or coordinates as 'latitude, longitude'.")
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1})
    data, _ = _get(url, 30)
    res = json.loads(data)
    if not res:
        raise TopoError(f"Place not found: '{query}'.")
    r = res[0]
    return float(r["lat"]), float(r["lon"]), r["display_name"]


# --- downloading the altitudes -----------------------------------------------------
def _bbox(lat, lon, size_m):
    half = size_m / 2
    dlat = half / M_PER_DEG_LAT
    dlon = half / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _extent_m(bbox):
    """(width, height) in meters of an extent (south, west, north, east)."""
    s, w, n, e = bbox
    return (e - w) * M_PER_DEG_LAT * math.cos(math.radians((s + n) / 2)), (n - s) * M_PER_DEG_LAT


def _grid_dims(bbox, step):
    """(columns, rows, real step) : at most MAX_GRID points on the long side."""
    sx, sy = _extent_m(bbox)
    st = max(step, max(sx, sy) / MAX_GRID)
    return max(8, round(sx / st)) + 1, max(8, round(sy / st)) + 1, st


def _fetch_ign(bbox, kind, step, progress):
    s, w, n, e = bbox
    W, H, st = _grid_dims(bbox, step)
    progress(0.1, "Downloading IGN data…")
    q = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": IGN_LAYERS[kind], "STYLES": "",
         "CRS": "EPSG:4326", "BBOX": f"{s},{w},{n},{e}", "WIDTH": W, "HEIGHT": H, "FORMAT": "image/x-bil;bits=32"}
    data, ctype = _get(IGN_WMS + "?" + urllib.parse.urlencode(q), 120)
    if "bil" not in ctype or len(data) != W * H * 4:
        raise TopoError("Unexpected answer from the IGN server: " + data[:200].decode("utf-8", "replace"))
    z = np.frombuffer(data, dtype="<f4").reshape(H, W).astype(np.float64)
    z[z < -1000] = np.nan
    return z[::-1], st  # ligne 0 = sud


def _fetch_aws(bbox, step, progress):
    s, w, n, e = bbox
    lat = (s + n) / 2
    # zoom whose ground step is closest to the wanted step (max z14 : beyond, no more useful detail)
    z = int(np.clip(round(math.log2(40075016.686 * math.cos(math.radians(lat)) / 256 / max(step, 1e-3))), 8, 14))

    def tile_xy(la, lo, nt):
        return (lo + 180) / 360 * nt, (1 - math.asinh(math.tan(math.radians(la))) / math.pi) / 2 * nt

    while True:
        nt = 2 ** z
        x0, y1 = tile_xy(s, w, nt)
        x1, y0 = tile_xy(n, e, nt)
        tx0, tx1, ty0, ty1 = int(x0), int(x1), int(y0), int(y1)
        cnt = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if cnt <= 64 or z <= 8:
            break
        z -= 1  # very large area : use less detailed tiles
    if cnt > 100:
        raise TopoError("Area too large for this source: reduce the size.")
    from PIL import Image
    big = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256))
    k = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            progress(0.1 + 0.6 * k / cnt, f"Downloading tiles {k + 1}/{cnt}…")
            data, _ = _get(AWS_TILES.format(z=z, x=tx, y=ty), 60)
            im = np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(np.float64)
            big[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = (
                im[..., 0] * 256 + im[..., 1] + im[..., 2] / 256 - 32768)
            k += 1
    # regular lat/lon grid, bilinear interpolation in the tile image
    res = 40075016.686 * math.cos(math.radians(lat)) / nt / 256
    W, H, st = _grid_dims(bbox, max(step, res))  # no point sampling below the native step
    px = np.array([(tile_xy(lat, lo, nt)[0] - tx0) * 256 - 0.5 for lo in np.linspace(w, e, W)])
    py = np.array([(tile_xy(la, w, nt)[1] - ty0) * 256 - 0.5 for la in np.linspace(s, n, H)])
    px = np.clip(px, 0, big.shape[1] - 1.001)
    py = np.clip(py, 0, big.shape[0] - 1.001)
    ix, iy = px.astype(int), py.astype(int)
    fx, fy = (px - ix)[None, :], (py - iy)[:, None]
    a, b = big[iy][:, ix], big[iy][:, ix + 1]
    c, d = big[iy + 1][:, ix], big[iy + 1][:, ix + 1]
    return a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy, max(st, res)


# --- satellite imagery (texture) ---------------------------------------------------
def world_xy(lat, lon, z):
    """'World pixel' coordinates (Web Mercator) at zoom z ; accepts numpy arrays."""
    n = TILE * 2.0 ** z
    lat = np.clip(lat, -85.0511, 85.0511)
    return (np.asarray(lon) + 180) / 360 * n, (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n


def fetch_imagery(bbox, layer, progress=lambda f, t: None, max_px=TEXTURE_MAX_PX, max_tiles=300):
    """Image of the extent (south, west, north, east) assembled from the tiles of the base map `layer`.
    Returns (PIL image, (x0, y0, x1, y1, z)) : 'world' coordinates of the image corner, to compute the UVs."""
    from PIL import Image
    s, w, n, e = bbox
    for z in range(19, 2, -1):
        (x0,), (y1,) = world_xy(s, w, z)[0:1], world_xy(s, w, z)[1:2]
        (x1,), (y0,) = world_xy(n, e, z)[0:1], world_xy(n, e, z)[1:2]
        tx0, tx1, ty0, ty1 = int(x0 // TILE), int(x1 // TILE), int(y0 // TILE), int(y1 // TILE)
        if max(x1 - x0, y1 - y0) <= max_px and (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= max_tiles:
            break
    nt = 2 ** z
    jobs = [(tx, ty) for ty in range(max(ty0, 0), min(ty1, nt - 1) + 1) for tx in range(tx0, tx1 + 1)]
    url, big, done, fails = TILE_LAYERS[layer][0], Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (90, 90, 90)), [0], [0]

    def one(job):
        tx, ty = job
        try:
            data, _ = _get(url.format(z=z, x=tx % nt, y=ty), 30)
            return job, Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:  # noqa: BLE001
            return job, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for (tx, ty), im in pool.map(one, jobs):
            done[0] += 1
            progress(done[0] / len(jobs), f"Satellite image: tile {done[0]}/{len(jobs)}…")
            if im is None:
                fails[0] += 1
            else:
                big.paste(im, ((tx - tx0) * TILE, (ty - ty0) * TILE))
    if fails[0] > len(jobs) / 2:
        raise TopoError("Could not download the satellite image.")
    ox, oy = tx0 * TILE, ty0 * TILE
    return big.crop((round(x0 - ox), round(y0 - oy), round(x1 - ox), round(y1 - oy))), (x0, y0, x1, y1, z)


def apply_texture(mesh, bbox, center, layer, progress=lambda f, t: None):
    """Drape the imagery over the mesh (UV per vertex). The exported DAE stays untextured :
    the AI importer ignores textures and accepts only one input per triangle."""
    img, (x0, y0, x1, y1, z) = fetch_imagery(bbox, layer, progress)
    path = os.path.join(tempfile.gettempdir(), "3D_to_AI_topo_%d.jpg" % (time.time() * 1000))
    img.save(path, quality=90)
    lat0, lon0 = center
    k = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    X, Y = world_xy(lat0 + mesh.verts[:, 1] / M_PER_DEG_LAT, lon0 + mesh.verts[:, 0] / k, z)
    uv = np.column_stack([(X - x0) / (x1 - x0), 1 - (Y - y0) / (y1 - y0)])
    name = "imagerie"
    return obj2dae.Mesh(verts=mesh.verts, tris=mesh.tris, tri_mat=np.zeros(len(mesh.tris), np.int32), mat_names=[name],
                        mat_colors={name: (0.5, 0.5, 0.5)}, vts=uv, tri_vt=mesh.tris.copy(), mat_tex={name: path})


# --- mesh ----------------------------------------------------------------------
def points_in_poly(px, py, poly):
    """Point-in-polygon test (ray casting), vectorised. poly : (k, 2)."""
    inside = np.zeros(px.shape, bool)
    x1, y1 = poly[-1]
    for x2, y2 in poly:
        cond = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = (x2 - x1) * (py - y1) / (y2 - y1) + x1
        inside ^= cond & (px < xi)
        x1, y1 = x2, y2
    return inside


def grid_to_mesh(z, size_xy, exaggeration=1.0, poly_xy=None):
    """Altitude grid (row 0 = south, NaN = no data) -> Mesh in meters, Z-up.
    poly_xy : polygon (meters, frame centered on the grid) ; only the triangles whose centre is
    inside are kept. Altitudes are coloured in bands (preview) ; the DAE keeps a single group."""
    H, W = z.shape
    sx, sy = size_xy
    xs = (np.arange(W) / (W - 1) - 0.5) * sx
    ys = (np.arange(H) / (H - 1) - 0.5) * sy
    X, Y = np.meshgrid(xs, ys)
    ok = np.isfinite(z)
    if not ok.any():
        raise TopoError("No altitude data for this area with this source.")
    zz = np.where(ok, z, 0.0) * exaggeration
    verts = np.column_stack([X.ravel(), Y.ravel(), zz.ravel()])
    i = (np.arange(H - 1)[:, None] * W + np.arange(W - 1)[None, :]).ravel()
    a, b, c, d = i, i + 1, i + W, i + W + 1
    # diagonal chosen by the smallest altitude difference : edges that follow the relief better
    zf = zz.ravel()
    flip = np.abs(zf[a] - zf[d]) > np.abs(zf[b] - zf[c])
    t1 = np.where(flip[:, None], np.column_stack([a, b, c]), np.column_stack([a, d, c]))
    t2 = np.where(flip[:, None], np.column_stack([b, d, c]), np.column_stack([a, b, d]))
    tris = np.concatenate([t1, t2])
    good = ok.ravel()
    tris = tris[good[tris].all(axis=1)]
    if poly_xy is not None:
        cen = verts[tris, :2].mean(axis=1)
        tris = tris[points_in_poly(cen[:, 0], cen[:, 1], np.asarray(poly_xy))]
        if not len(tris):
            raise TopoError("The polygon is too small to hold a triangle: enlarge the area.")
    # remove unused vertices
    used = np.zeros(len(verts), bool)
    used[tris.ravel()] = True
    remap = np.cumsum(used) - 1
    verts, tris = verts[used], remap[tris]
    # altitude bands -> preview 'materials'
    zc = verts[tris, 2].mean(axis=1)
    lo, hi = float(zc.min()), float(zc.max())
    band = np.clip(((zc - lo) / max(hi - lo, 1e-6) * N_BANDS).astype(int), 0, N_BANDS - 1)
    names = [f"alt{k:02d}" for k in range(N_BANDS)]
    colors = {nm: _hypso(k / (N_BANDS - 1)) for k, nm in enumerate(names)}
    return obj2dae.Mesh(verts=verts, tris=tris.astype(np.int64), tri_mat=band.astype(np.int32),
                        mat_names=names, mat_colors=colors)


def _hypso(t):
    """Hypsometric tint (green -> ochre -> brown -> white), components 0-1."""
    stops = [(0.0, (0.35, 0.58, 0.32)), (0.35, (0.72, 0.74, 0.40)), (0.65, (0.62, 0.46, 0.30)), (1.0, (0.95, 0.95, 0.95))]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            u = (t - t0) / (t1 - t0)
            return tuple(c0[j] + (c1[j] - c0[j]) * u for j in range(3))
    return stops[-1][1]


def _build(bbox, label, source, kind, step, exaggeration, poly_ll, progress, texture=None):
    sx, sy = _extent_m(bbox)
    if not 20 <= max(sx, sy) <= 20000:
        raise TopoError("The area must be between 20 m and 20 km on a side.")
    lat, lon = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    mns = kind == KINDS[1]
    auto_step = max(max(sx, sy) / MAX_GRID, 1.0)
    z = None
    if source in (SOURCES[0], SOURCES[1]):
        try:
            z, res = _fetch_ign(bbox, "mns" if mns else "mnt", step or auto_step, progress)
            if np.isfinite(z).mean() < 0.5:
                z = None
                if source == SOURCES[1]:
                    raise TopoError("No IGN data here (IGN only covers France and its overseas territories).")
        except TopoError:
            if source == SOURCES[1]:
                raise
            z = None  # Auto : fall back to the worldwide source
        if z is not None:
            used = "IGN " + ("MNS (surface)" if mns else "RGE ALTI (terrain)")
    if z is None:
        if mns:
            raise TopoError("The surface model (buildings, trees) only exists for France (IGN).")
        z, res = _fetch_aws(bbox, step or max(auto_step, 3.0), progress)
        used = "AWS Terrain Tiles"
    progress(0.8, "Building the mesh…")
    poly_xy = None
    if poly_ll is not None:
        k = M_PER_DEG_LAT * math.cos(math.radians(lat))
        poly_xy = [((lo - lon) * k, (la - lat) * M_PER_DEG_LAT) for la, lo in poly_ll]
    mesh = grid_to_mesh(z, (sx, sy), exaggeration, poly_xy)
    area = _poly_area(poly_xy) if poly_xy else sx * sy
    info = {"label": label, "lat": lat, "lon": lon, "source": used, "step": res, "area": area,
            "zmin": float(np.nanmin(z)), "zmax": float(np.nanmax(z)), "size": (sx, sy), "texture": None}
    if texture:
        try:
            mesh = apply_texture(mesh, bbox, (lat, lon), texture, lambda f, t: progress(0.82 + 0.16 * f, t))
            info["texture"] = texture
        except Exception as e:  # noqa: BLE001  (the terrain stays usable without the image)
            info["texture_error"] = str(e)
    return mesh, info


def _poly_area(xy):
    x, y = np.asarray(xy).T
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2


def poly_bbox(poly_ll):
    """Extent (south, west, north, east) of a polygon [(lat, lon), …]."""
    la, lo = zip(*poly_ll)
    return min(la), min(lo), max(la), max(lo)


def poly_extent_m(poly_ll):
    """(width, height, area) in meters / m² of a polygon [(lat, lon), …]."""
    bb = poly_bbox(poly_ll)
    sx, sy = _extent_m(bb)
    k = M_PER_DEG_LAT * math.cos(math.radians((bb[0] + bb[2]) / 2))
    return sx, sy, _poly_area([(lo * k, la * M_PER_DEG_LAT) for la, lo in poly_ll])


def build(place, size_m=500.0, source=SOURCES[0], kind=KINDS[0], step=0.0, exaggeration=1.0,
          progress=lambda f, t: None, texture=None):
    """Square area of `size_m` around a place. texture = name of a base map (TILE_LAYERS) to drape. step = grid step in m (0 = auto).
    Returns (Mesh, info) ; info : place, centre, source used, real step, altitudes."""
    size_m = float(size_m)
    if not 20 <= size_m <= 20000:
        raise TopoError("The area size must be between 20 m and 20 km.")
    progress(0.02, "Looking up the place…")
    lat, lon, label = geocode(place)
    return _build(_bbox(lat, lon, size_m), label, source, kind, step, exaggeration, None, progress, texture)


def build_polygon(poly_ll, label="Drawn area", source=SOURCES[0], kind=KINDS[0], step=0.0, exaggeration=1.0,
                  progress=lambda f, t: None, texture=None):
    """Area bounded by a polygon [(lat, lon), …] (≥ 3 points)."""
    if len(poly_ll) < 3:
        raise TopoError("The polygon needs at least 3 points.")
    return _build(poly_bbox(poly_ll), label, source, kind, step, exaggeration, list(poly_ll), progress, texture)


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Topographic survey -> .dae")
    ap.add_argument("place", help="address, place or 'lat, lon'")
    ap.add_argument("dst")
    ap.add_argument("--size", type=float, default=500, help="side of the area in m")
    ap.add_argument("--source", type=int, choices=(0, 1, 2), default=0)
    ap.add_argument("--surface", action="store_true", help="DSM (buildings, trees) instead of bare terrain")
    ap.add_argument("--max-tris", type=int, default=200000)
    a = ap.parse_args()
    t0 = time.time()
    m, inf = build(a.place, a.size, SOURCES[a.source], KINDS[1 if a.surface else 0],
                   progress=lambda f, t: print(f"  {f:4.0%} {t}"))
    print(inf)
    n_in = len(m.tris)
    m = obj2dae.decimate(m, a.max_tris)
    m.verts[:, 2] -= m.verts[:, 2].min()
    obj2dae.write_dae(m, a.dst, "Z", True, True)
    print(f"{n_in} -> {len(m.tris)} triangles in {time.time() - t0:.1f}s")
