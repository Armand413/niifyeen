"""Importe la carte d'Ouagadougou, crée les caméras et les place sur la carte, en une seule commande.

Serveur lancé (voir README), puis, dans PowerShell, depuis la racine du projet :

    .\\venv\\Scripts\\python.exe tools\\city_map.py --sources 0,1,rtsp://ip2/flux,http://ip3:8080/video

--sources : 4 sources vidéo au plus (index de webcam, ou adresse RTSP / HTTP), une par repère de la carte. Les caméras
« cam1 »… sont créées (ou mises à jour si elles existent déjà) puis placées. Sans --sources, seules les caméras déjà
enregistrées (--cameras id1,id2,...) sont placées.
Carte : par défaut cartes-locales/ouagadougou-google.png (capture Google Maps, positions repérées à la main).
Autre carte : image exportée d'OpenStreetMap + --bbox ouest,sud,est,nord (projection Web Mercator, positions par GPS).

Les emplacements sont INDICATIFS : les ajuster en déplaçant les caméras dans la page Carte.
Fond OpenStreetMap : © les contributeurs d'OpenStreetMap (ODbL). Capture Google : droits Google, ne pas publier dans le dépôt.
"""
import argparse
import json
import math
import re
import struct
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_BBOX = (-1.56, 12.33, -1.47, 12.40)
# (libellé, lieu, latitude, longitude, direction en degrés, 0 = haut) : repères approximatifs d'Ouagadougou
SPOTS = [
    ("Place de la Nation", "Centre-ville", 12.3714, -1.5197, 90),
    ("Marché Rood Woko", "Grand marché", 12.3655, -1.5253, 0),
    ("Aéroport", "Aéroport international", 12.3532, -1.5124, 270),
    ("Université Joseph Ki-Zerbo", "Campus", 12.3810, -1.4990, 180),
]
# Variante « capture d'écran » (--pixels) : positions repérées à la main sur la capture Google Maps d'Ouagadougou de 1546 x 865 px.
# (libellé, lieu, x en px, y en px, direction)
PIXEL_REF = (1546, 865)
PIXEL_SPOTS = [
    ("Centre-ville", "Cœur de ville", 845, 505, 90),
    ("Université Joseph Ki-Zerbo", "Campus", 1000, 395, 180),
    ("Stade du 4 Août", "Stade", 459, 473, 90),
    ("Aéroport Thomas Sankara", "Aéroport international", 921, 636, 0),
]
DEFAULT_IMAGE = "cartes-locales/ouagadougou-google.png"
FOV, RANGE = 90.0, 0.05      # ouverture en degrés ; portée en fraction de la largeur de l'image


def _merc_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def to_xy(lat: float, lon: float, bbox) -> tuple:
    """Position relative (0-1) dans une image Web Mercator couvrant bbox = (ouest, sud, est, nord)."""
    west, south, east, north = bbox
    x = (lon - west) / (east - west)
    y = (_merc_y(north) - _merc_y(lat)) / (_merc_y(north) - _merc_y(south))
    return x, y


def _request(method: str, url: str, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Serveur injoignable ({exc.reason}). Lancer d'abord l'application.")


def _multipart(fields: dict, filename: str, data: bytes) -> tuple:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image", nargs="?", default=DEFAULT_IMAGE, help="image de la carte (PNG ou JPEG)")
    ap.add_argument("--sources", default="", help="sources vidéo des caméras à créer, séparées par des virgules")
    ap.add_argument("--bbox", default=",".join(map(str, DEFAULT_BBOX)), help="ouest,sud,est,nord de l'image")
    ap.add_argument("--cameras", default="", help="identifiants des caméras à placer, dans l'ordre des repères")
    ap.add_argument("--pixels", action="store_true", help="placer les caméras selon les positions repérées sur la capture Google Maps (sans GPS)")
    ap.add_argument("--name", default="Ouagadougou")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    try:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
    except (ValueError, AssertionError):
        raise SystemExit("--bbox attendu : ouest,sud,est,nord (ouest < est, sud < nord).")
    sources = [c.strip() for c in args.sources.split(",") if c.strip()]
    ids = [c.strip() for c in args.cameras.split(",") if c.strip()] or [f"cam{k + 1}" for k in range(len(sources))]
    if not ids:
        raise SystemExit("Indiquer --sources (caméras à créer) ou --cameras (caméras déjà enregistrées).")
    if len(ids) > len(SPOTS):
        raise SystemExit(f"{len(SPOTS)} caméras au plus.")
    if args.image.replace("\\", "/").endswith("ouagadougou-google.png"):
        args.pixels = True
    try:
        data = open(args.image, "rb").read()
    except OSError as exc:
        raise SystemExit(f"Image introuvable : {exc}")
    st, floors = _request("GET", args.url + "/api/floors")
    existing = [f for f in (floors if isinstance(floors, list) else floors.get("floors", []) if isinstance(floors, dict) else []) if f.get("name") == args.name]
    if existing:
        floor = existing[0]
        print(f"Étage « {args.name} » déjà présent : image conservée (le supprimer dans la page Carte pour la remplacer).")
        status = 200
    else:
        floor = None
    body, ctype = _multipart({"name": args.name, "level": "0"}, args.image.replace("\\", "/").rsplit("/", 1)[-1], data)
    if floor is None:
        status, floor = _request("POST", args.url + "/api/floors", body, {"Content-Type": ctype})
        if status not in (200, 201):
            raise SystemExit(f"Import refusé ({status}) : {floor}")
    st, cams = _request("GET", args.url + "/api/admin/cameras")
    known = {c.get("id") for c in (cams if isinstance(cams, list) else cams.get("cameras", []) if isinstance(cams, dict) else [])}
    placed = 0
    spots = ([(lb, li, px / PIXEL_REF[0], py / PIXEL_REF[1], h) for lb, li, px, py, h in PIXEL_SPOTS] if args.pixels
             else [(lb, li) + to_xy(lat, lon, bbox) + (h,) for lb, li, lat, lon, h in SPOTS])
    ids = list(ids)
    for k, (cid, (label, lieu, x, y, head)) in enumerate(zip(list(ids), spots)):
        if k < len(sources) and cid not in known:
            st, res = _request("POST", args.url + "/api/admin/cameras", json.dumps(
                {"id": cid, "label": label, "location": lieu, "source": sources[k]}).encode(), {"Content-Type": "application/json"})
            if st == 201:
                print(f"  créée  {cid} (source {sources[k]})")
            else:
                msg = json.dumps(res, ensure_ascii=False) if not isinstance(res, str) else res
                other = re.search(r"« ([^»]+) »", msg) if "déjà utilisée" in msg else None
                if other:                                   # ex. webcam 0 déjà enregistrée au premier démarrage : on la place à la place
                    name = other.group(1).strip()
                    st2, cl = _request("GET", args.url + "/api/admin/cameras")
                    rows = cl.get("cameras", []) if isinstance(cl, dict) else []
                    cid = next((c["id"] for c in rows if c.get("id") == name or c.get("label") == name), name)
                    print(f"  source {sources[k]} déjà utilisée par la caméra « {cid} » : elle est placée à la place.")
                else:
                    print(f"  ERREUR création {cid} (source {sources[k]}) : {msg}")
                    continue
        elif k < len(sources):
            _request("PUT", f"{args.url}/api/admin/cameras/{cid}", json.dumps({"source": sources[k]}).encode(), {"Content-Type": "application/json"})
        if not (0 <= x <= 1 and 0 <= y <= 1):
            print(f"  ignorée : {label} est hors de l'image")
            continue
        payload = {"floor_id": floor["id"], "x": round(x, 4), "y": round(y, 4), "heading": head, "fov": FOV,
                   "view_range": RANGE, "label": label, "location": lieu}
        st, res = _request("PUT", f"{args.url}/api/admin/cameras/{cid}", json.dumps(payload).encode(),
                           {"Content-Type": "application/json"})
        print(f"  {'OK ' if st == 200 else 'ERR'} {cid} -> {label} ({x:.3f}, {y:.3f})" + ("" if st == 200 else f" : {res}"))
        placed += st == 200
    print(f"Carte « {args.name} » importée, {placed} caméra(s) placée(s). Ouvrir {args.url}/map")
    return 0 if placed else 1


if __name__ == "__main__":
    sys.exit(main())
