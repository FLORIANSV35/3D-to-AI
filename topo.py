"""Relevé topographique depuis internet -> maillage 3D (sans interface).

Sources (gratuites, sans clé) :
  - IGN Géoplateforme (France) : RGE ALTI (MNT, sol nu, ~1 m) ou MNS (surface : bâtiments, arbres) ;
  - AWS Terrain Tiles (monde entier, SRTM/NASADEM/LiDAR selon les pays, 3 à 30 m) ;
  - OpenStreetMap Nominatim pour retrouver un lieu à partir d'une adresse.

Le maillage produit est en mètres, repère local « Z vertical », centré sur le lieu choisi
(X = est, Y = nord, Z = altitude).
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

UA = "DRONE-TO-AI/1.0 (convertisseur OBJ->DAE pour Adamson ArrayIntelligence)"
IGN_WMS = "https://data.geopf.fr/wms-r/wms"
IGN_LAYERS = {
    "mnt": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES",
    "mns": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES.MNS",
}
AWS_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
NODATA = -99999.0
M_PER_DEG_LAT = 111132.0
MAX_GRID = 1500  # points par côté au maximum (IGN accepte 5000, mais le maillage deviendrait énorme)
N_BANDS = 12  # tranches d'altitude colorées (aperçu seulement)

TILE = 256
TILE_LAYERS = {  # nom -> (modèle d'URL de tuile, mention de source)
    "Satellite (Esri)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Imagery © Esri, Maxar, Earthstar Geographics"),
    "Orthophoto IGN (France)": (
        "https://data.geopf.fr/wmts?SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile&LAYER=ORTHOIMAGERY.ORTHOPHOTOS"
        "&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg",
        "© IGN – Géoplateforme"),
    "Plan (OpenStreetMap)": (
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "© contributeurs OpenStreetMap"),
}
TEXTURE_MAX_PX = 4096  # côté maximal de l'image plaquée sur le terrain
SOURCES = ("Auto (IGN puis monde)", "IGN France (1 m)", "Monde (AWS, 3-30 m)")
KINDS = ("Terrain nu (MNT)", "Surface : bâtiments, arbres (MNS, IGN)")


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
        raise TopoError(f"Connexion impossible ({e}). Vérifie ta connexion internet.") from e


# --- lieu ---------------------------------------------------------------------------
_LATLON = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d+(?:[.,]\d+)?)\s*$")


def geocode(query):
    """Adresse / nom de lieu / « lat, lon » -> (lat, lon, libellé)."""
    m = _LATLON.match(query)
    if m:
        lat, lon = (float(g.replace(",", ".")) for g in m.groups())
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise TopoError("Coordonnées hors limites (latitude, longitude).")
        return lat, lon, f"{lat:.5f}, {lon:.5f}"
    if not query.strip():
        raise TopoError("Indique un lieu (adresse, ville…) ou des coordonnées « latitude, longitude ».")
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1})
    data, _ = _get(url, 30)
    res = json.loads(data)
    if not res:
        raise TopoError(f"Lieu introuvable : « {query} ».")
    r = res[0]
    return float(r["lat"]), float(r["lon"]), r["display_name"]


# --- téléchargement des altitudes -----------------------------------------------------
def _bbox(lat, lon, size_m):
    half = size_m / 2
    dlat = half / M_PER_DEG_LAT
    dlon = half / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _extent_m(bbox):
    """(largeur, hauteur) en mètres d'une emprise (sud, ouest, nord, est)."""
    s, w, n, e = bbox
    return (e - w) * M_PER_DEG_LAT * math.cos(math.radians((s + n) / 2)), (n - s) * M_PER_DEG_LAT


def _grid_dims(bbox, step):
    """(colonnes, lignes, pas réel) : au plus MAX_GRID points sur le grand côté."""
    sx, sy = _extent_m(bbox)
    st = max(step, max(sx, sy) / MAX_GRID)
    return max(8, round(sx / st)) + 1, max(8, round(sy / st)) + 1, st


def _fetch_ign(bbox, kind, step, progress):
    s, w, n, e = bbox
    W, H, st = _grid_dims(bbox, step)
    progress(0.1, "Téléchargement IGN…")
    q = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": IGN_LAYERS[kind], "STYLES": "",
         "CRS": "EPSG:4326", "BBOX": f"{s},{w},{n},{e}", "WIDTH": W, "HEIGHT": H, "FORMAT": "image/x-bil;bits=32"}
    data, ctype = _get(IGN_WMS + "?" + urllib.parse.urlencode(q), 120)
    if "bil" not in ctype or len(data) != W * H * 4:
        raise TopoError("Réponse inattendue du serveur IGN : " + data[:200].decode("utf-8", "replace"))
    z = np.frombuffer(data, dtype="<f4").reshape(H, W).astype(np.float64)
    z[z < -1000] = np.nan
    return z[::-1], st  # ligne 0 = sud


def _fetch_aws(bbox, step, progress):
    s, w, n, e = bbox
    lat = (s + n) / 2
    # zoom dont le pas au sol est le plus proche du pas voulu (max z14 : au-delà, pas plus de détail utile)
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
        z -= 1  # zone très grande : on prend des tuiles moins détaillées
    if cnt > 100:
        raise TopoError("Zone trop grande pour cette source : réduis la taille.")
    from PIL import Image
    big = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256))
    k = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            progress(0.1 + 0.6 * k / cnt, f"Téléchargement des tuiles {k + 1}/{cnt}…")
            data, _ = _get(AWS_TILES.format(z=z, x=tx, y=ty), 60)
            im = np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(np.float64)
            big[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = (
                im[..., 0] * 256 + im[..., 1] + im[..., 2] / 256 - 32768)
            k += 1
    # grille régulière en lat/lon, interpolation bilinéaire dans l'image des tuiles
    res = 40075016.686 * math.cos(math.radians(lat)) / nt / 256
    W, H, st = _grid_dims(bbox, max(step, res))  # inutile d'échantillonner sous le pas natif
    px = np.array([(tile_xy(lat, lo, nt)[0] - tx0) * 256 - 0.5 for lo in np.linspace(w, e, W)])
    py = np.array([(tile_xy(la, w, nt)[1] - ty0) * 256 - 0.5 for la in np.linspace(s, n, H)])
    px = np.clip(px, 0, big.shape[1] - 1.001)
    py = np.clip(py, 0, big.shape[0] - 1.001)
    ix, iy = px.astype(int), py.astype(int)
    fx, fy = (px - ix)[None, :], (py - iy)[:, None]
    a, b = big[iy][:, ix], big[iy][:, ix + 1]
    c, d = big[iy + 1][:, ix], big[iy + 1][:, ix + 1]
    return a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy, max(st, res)


# --- imagerie satellite (texture) ---------------------------------------------------
def world_xy(lat, lon, z):
    """Coordonnées « pixels du monde » (Web Mercator) au zoom z ; accepte des tableaux numpy."""
    n = TILE * 2.0 ** z
    lat = np.clip(lat, -85.0511, 85.0511)
    return (np.asarray(lon) + 180) / 360 * n, (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n


def fetch_imagery(bbox, layer, progress=lambda f, t: None, max_px=TEXTURE_MAX_PX, max_tiles=300):
    """Image de l'emprise (sud, ouest, nord, est) assemblée depuis les tuiles du fond de carte `layer`.
    Retourne (image PIL, (x0, y0, x1, y1, z)) : coordonnées « monde » du coin de l'image, pour calculer les UV."""
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
            progress(done[0] / len(jobs), f"Image satellite : tuile {done[0]}/{len(jobs)}…")
            if im is None:
                fails[0] += 1
            else:
                big.paste(im, ((tx - tx0) * TILE, (ty - ty0) * TILE))
    if fails[0] > len(jobs) / 2:
        raise TopoError("Impossible de télécharger l'image satellite.")
    ox, oy = tx0 * TILE, ty0 * TILE
    return big.crop((round(x0 - ox), round(y0 - oy), round(x1 - ox), round(y1 - oy))), (x0, y0, x1, y1, z)


def apply_texture(mesh, bbox, center, layer, progress=lambda f, t: None):
    """Plaque l'imagerie sur le maillage (UV par sommet). Le DAE exporté, lui, reste sans texture :
    l'importeur d'AI ignore les textures et n'accepte qu'une entrée par triangle."""
    img, (x0, y0, x1, y1, z) = fetch_imagery(bbox, layer, progress)
    path = os.path.join(tempfile.gettempdir(), "DRONE_TO_AI_topo_%d.jpg" % (time.time() * 1000))
    img.save(path, quality=90)
    lat0, lon0 = center
    k = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    X, Y = world_xy(lat0 + mesh.verts[:, 1] / M_PER_DEG_LAT, lon0 + mesh.verts[:, 0] / k, z)
    uv = np.column_stack([(X - x0) / (x1 - x0), 1 - (Y - y0) / (y1 - y0)])
    name = "imagerie"
    return obj2dae.Mesh(verts=mesh.verts, tris=mesh.tris, tri_mat=np.zeros(len(mesh.tris), np.int32), mat_names=[name],
                        mat_colors={name: (0.5, 0.5, 0.5)}, vts=uv, tri_vt=mesh.tris.copy(), mat_tex={name: path})


# --- maillage ----------------------------------------------------------------------
def points_in_poly(px, py, poly):
    """Test point-dans-polygone (lancer de rayon), vectorisé. poly : (k, 2)."""
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
    """Grille d'altitudes (ligne 0 = sud, NaN = pas de donnée) -> Mesh en mètres, Z vertical.
    poly_xy : polygone (mètres, repère centré sur la grille) ; seuls les triangles dont le centre est
    dedans sont gardés. Les altitudes sont en tranches colorées (aperçu) ; le DAE reste à un seul groupe."""
    H, W = z.shape
    sx, sy = size_xy
    xs = (np.arange(W) / (W - 1) - 0.5) * sx
    ys = (np.arange(H) / (H - 1) - 0.5) * sy
    X, Y = np.meshgrid(xs, ys)
    ok = np.isfinite(z)
    if not ok.any():
        raise TopoError("Aucune donnée d'altitude pour cette zone avec cette source.")
    zz = np.where(ok, z, 0.0) * exaggeration
    verts = np.column_stack([X.ravel(), Y.ravel(), zz.ravel()])
    i = (np.arange(H - 1)[:, None] * W + np.arange(W - 1)[None, :]).ravel()
    a, b, c, d = i, i + 1, i + W, i + W + 1
    # diagonale choisie selon la plus petite différence d'altitude : arêtes plus fidèles au relief
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
            raise TopoError("Le polygone est trop petit pour contenir un triangle : agrandis la zone.")
    # retire les sommets inutilisés
    used = np.zeros(len(verts), bool)
    used[tris.ravel()] = True
    remap = np.cumsum(used) - 1
    verts, tris = verts[used], remap[tris]
    # tranches d'altitude -> « matériaux » de l'aperçu
    zc = verts[tris, 2].mean(axis=1)
    lo, hi = float(zc.min()), float(zc.max())
    band = np.clip(((zc - lo) / max(hi - lo, 1e-6) * N_BANDS).astype(int), 0, N_BANDS - 1)
    names = [f"alt{k:02d}" for k in range(N_BANDS)]
    colors = {nm: _hypso(k / (N_BANDS - 1)) for k, nm in enumerate(names)}
    return obj2dae.Mesh(verts=verts, tris=tris.astype(np.int64), tri_mat=band.astype(np.int32),
                        mat_names=names, mat_colors=colors)


def _hypso(t):
    """Teinte hypsométrique (vert -> ocre -> brun -> blanc), composantes 0-1."""
    stops = [(0.0, (0.35, 0.58, 0.32)), (0.35, (0.72, 0.74, 0.40)), (0.65, (0.62, 0.46, 0.30)), (1.0, (0.95, 0.95, 0.95))]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            u = (t - t0) / (t1 - t0)
            return tuple(c0[j] + (c1[j] - c0[j]) * u for j in range(3))
    return stops[-1][1]


def _build(bbox, label, source, kind, step, exaggeration, poly_ll, progress, texture=None):
    sx, sy = _extent_m(bbox)
    if not 20 <= max(sx, sy) <= 20000:
        raise TopoError("La zone doit mesurer entre 20 m et 20 km de côté.")
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
                    raise TopoError("Pas de données IGN à cet endroit (l'IGN ne couvre que la France et l'outre-mer).")
        except TopoError:
            if source == SOURCES[1]:
                raise
            z = None  # Auto : on bascule sur la source mondiale
        if z is not None:
            used = "IGN " + ("MNS (surface)" if mns else "RGE ALTI (terrain)")
    if z is None:
        if mns:
            raise TopoError("Le modèle de surface (bâtiments, arbres) n'existe que pour la France (IGN).")
        z, res = _fetch_aws(bbox, step or max(auto_step, 3.0), progress)
        used = "AWS Terrain Tiles"
    progress(0.8, "Construction du maillage…")
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
        except Exception as e:  # noqa: BLE001  (le relief reste utilisable sans image)
            info["texture_error"] = str(e)
    return mesh, info


def _poly_area(xy):
    x, y = np.asarray(xy).T
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2


def poly_bbox(poly_ll):
    """Emprise (sud, ouest, nord, est) d'un polygone [(lat, lon), …]."""
    la, lo = zip(*poly_ll)
    return min(la), min(lo), max(la), max(lo)


def poly_extent_m(poly_ll):
    """(largeur, hauteur, aire) en mètres / m² d'un polygone [(lat, lon), …]."""
    bb = poly_bbox(poly_ll)
    sx, sy = _extent_m(bb)
    k = M_PER_DEG_LAT * math.cos(math.radians((bb[0] + bb[2]) / 2))
    return sx, sy, _poly_area([(lo * k, la * M_PER_DEG_LAT) for la, lo in poly_ll])


def build(place, size_m=500.0, source=SOURCES[0], kind=KINDS[0], step=0.0, exaggeration=1.0,
          progress=lambda f, t: None, texture=None):
    """Zone carrée de `size_m` autour d'un lieu. texture = nom d'un fond de carte (TILE_LAYERS) à plaquer. step = pas de grille en m (0 = auto).
    Retourne (Mesh, infos) ; infos : lieu, centre, source utilisée, pas réel, altitudes."""
    size_m = float(size_m)
    if not 20 <= size_m <= 20000:
        raise TopoError("La taille de la zone doit être comprise entre 20 m et 20 km.")
    progress(0.02, "Recherche du lieu…")
    lat, lon, label = geocode(place)
    return _build(_bbox(lat, lon, size_m), label, source, kind, step, exaggeration, None, progress, texture)


def build_polygon(poly_ll, label="Zone dessinée", source=SOURCES[0], kind=KINDS[0], step=0.0, exaggeration=1.0,
                  progress=lambda f, t: None, texture=None):
    """Zone délimitée par un polygone [(lat, lon), …] (≥ 3 points)."""
    if len(poly_ll) < 3:
        raise TopoError("Le polygone doit avoir au moins 3 points.")
    return _build(poly_bbox(poly_ll), label, source, kind, step, exaggeration, list(poly_ll), progress, texture)


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Relevé topographique -> .dae")
    ap.add_argument("place", help="adresse, lieu ou « lat, lon »")
    ap.add_argument("dst")
    ap.add_argument("--size", type=float, default=500, help="côté de la zone en m")
    ap.add_argument("--source", type=int, choices=(0, 1, 2), default=0)
    ap.add_argument("--surface", action="store_true", help="MNS (bâtiments, arbres) au lieu du terrain nu")
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
    print(f"{n_in} -> {len(m.tris)} triangles en {time.time() - t0:.1f}s")
