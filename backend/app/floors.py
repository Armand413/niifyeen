"""Étages et plans du site.

Un plan est une image PNG/JPEG (ré-encodée en PNG : métadonnées retirées, taille bornée) ou un SVG assaini.
Les SVG sont servis avec une politique de sécurité qui interdit tout script, et affichés uniquement via une
balise ``<img>`` : même un SVG piégé ne peut rien exécuter. Par précaution, tout SVG contenant du script,
des gestionnaires d'événements, des objets externes ou des références externes est refusé.
"""
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from .db import Camera, Floor, to_iso_utc

MAX_UPLOAD_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 2400                 # plus grand côté après ré-encodage
MAX_FLOORS = 12
MAX_NAME = 40
SVG_DEFAULT = (900, 510)


class FloorError(ValueError):
    def __init__(self, message: str, field: Optional[str] = None, status: int = 400):
        super().__init__(message)
        self.field, self.status = field, status


class FloorNotFound(FloorError):
    def __init__(self):
        super().__init__("Étage introuvable.", status=404)


def _kind(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<"):
        return "svg"
    raise FloorError("Format non pris en charge : utilisez PNG, JPEG ou SVG.", "file")


_FORBIDDEN_TAGS = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video", "animate", "set"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _svg_size(root) -> Tuple[int, int]:
    vb = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        if len(vb) == 4 and float(vb[2]) > 0 and float(vb[3]) > 0:
            return max(1, round(float(vb[2]))), max(1, round(float(vb[3])))
    except ValueError:
        pass
    def num(v):
        m = re.match(r"^\s*([0-9.]+)(px)?\s*$", v or "")
        return float(m.group(1)) if m else 0
    w, h = num(root.get("width")), num(root.get("height"))
    if w > 0 and h > 0:
        return round(w), round(h)
    raise FloorError("Le SVG doit avoir un attribut viewBox (ou width et height).", "file")


def check_svg(data: bytes) -> Tuple[int, int]:
    """Refuse un SVG dangereux ou illisible ; retourne ses dimensions."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FloorError("Le SVG doit être encodé en UTF-8.", "file")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise FloorError("SVG refusé : déclarations DOCTYPE / ENTITY interdites.", "file")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise FloorError("SVG illisible.", "file")
    if _local(root.tag) != "svg":
        raise FloorError("Ce fichier n'est pas un SVG.", "file")
    for node in root.iter():
        if _local(node.tag) in _FORBIDDEN_TAGS:
            raise FloorError(f"SVG refusé : balise « {_local(node.tag)} » interdite.", "file")
        for name, value in node.attrib.items():
            key = _local(name)
            if key.startswith("on"):
                raise FloorError("SVG refusé : gestionnaires d'événements interdits.", "file")
            if key in ("href", "src") or name.endswith("}href"):
                if not value.strip().startswith("#"):
                    raise FloorError("SVG refusé : références externes interdites.", "file")
            if re.search(r"(javascript:|url\(\s*['\"]?\s*(https?:|data:|//))", value, re.I):
                raise FloorError("SVG refusé : contenu externe ou script.", "file")
        if _local(node.tag) == "style" and node.text and re.search(r"@import|url\(", node.text, re.I):
            raise FloorError("SVG refusé : feuille de style avec référence externe.", "file")
    return _svg_size(root)


def prepare_image(data: bytes) -> Dict[str, Any]:
    """-> {mime, image, width, height} prêt à être stocké."""
    if not data:
        raise FloorError("Fichier vide.", "file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise FloorError(f"Fichier trop volumineux (maximum {MAX_UPLOAD_BYTES // (1024 * 1024)} Mo).", "file", 413)
    kind = _kind(data)
    if kind == "svg":
        w, h = check_svg(data)
        return {"mime": "image/svg+xml", "image": data, "width": w, "height": h}
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise FloorError("Image illisible.", "file")
    h, w = img.shape[:2]
    scale = min(1.0, MAX_PIXELS / float(max(h, w)))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise FloorError("Image impossible à encoder.", "file")
    return {"mime": "image/png", "image": buf.tobytes(), "width": int(w), "height": int(h)}


def clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FloorError("Le nom de l'étage est obligatoire.", "name")
    name = value.strip()
    if len(name) > MAX_NAME:
        raise FloorError(f"Le nom est limité à {MAX_NAME} caractères.", "name")
    return name


def clean_level(value: Any) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        raise FloorError("Le niveau doit être un entier.", "level")
    if not -10 <= level <= 100:
        raise FloorError("Le niveau doit être compris entre -10 et 100.", "level")
    return level


def view(floor: Floor, cameras: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"id": floor.id, "name": floor.name, "level": floor.level, "mime": floor.mime,
            "width": floor.width, "height": floor.height, "image_url": f"/api/floors/{floor.id}/image",
            "created_at": to_iso_utc(floor.created_at) if floor.created_at else None,
            "cameras": cameras or []}


def list_floors(db) -> List[Dict[str, Any]]:
    by_floor: Dict[int, List[str]] = {}
    for cam_id, floor_id in db.query(Camera.id, Camera.floor_id).filter(Camera.floor_id.isnot(None)).order_by(Camera.position):
        by_floor.setdefault(floor_id, []).append(cam_id)
    rows = db.query(Floor).order_by(Floor.level, Floor.id).all()
    return [view(f, by_floor.get(f.id)) for f in rows]


COVERAGE_COLS = 80                # résolution maximale de la grille de couverture (colonnes)
COVERAGE_DEFAULTS = {"heading": 0.0, "fov": 70.0, "view_range": 0.18}


def coverage(db, floor_id: int, cols: int = COVERAGE_COLS) -> Dict[str, Any]:
    """Part du plan vue par au moins une caméra, zones vues par plusieurs, angles morts.

    Calcul géométrique pur sur une grille : chaque caméra active placée sur l'étage couvre un secteur
    (position, direction 0° = haut du plan sens horaire, ouverture, portée = fraction de la largeur du plan).
    Les MURS ET OBSTACLES NE SONT PAS PRIS EN COMPTE : c'est une borne haute de la couverture réelle.
    Les valeurs absentes prennent celles par défaut de la page Carte (comme le dessin du secteur).
    """
    floor = get(db, floor_id)
    cols = max(10, min(int(cols), COVERAGE_COLS))
    w, h = float(floor.width), float(floor.height)
    rows = max(10, min(COVERAGE_COLS, int(round(cols * h / w))))
    cams = (db.query(Camera).filter(Camera.floor_id == floor_id, Camera.enabled.is_(True),
                                    Camera.x.isnot(None), Camera.y.isnot(None)).order_by(Camera.position).all())
    ex = db.query(Camera.id).filter(Camera.floor_id == floor_id, Camera.enabled.is_(False)).count()
    xs = (np.arange(cols) + 0.5) / cols * w                       # centres des cellules, en pixels du plan
    ys = (np.arange(rows) + 0.5) / rows * h
    gx, gy = np.meshgrid(xs, ys)
    count = np.zeros((rows, cols), dtype=np.int16)
    masks = {}
    for c in cams:
        fov = c.fov if c.fov is not None else COVERAGE_DEFAULTS["fov"]
        rng = (c.view_range if c.view_range is not None else COVERAGE_DEFAULTS["view_range"]) * w
        head = c.heading if c.heading is not None else COVERAGE_DEFAULTS["heading"]
        dx, dy = gx - c.x * w, gy - c.y * h
        dist = np.hypot(dx, dy)
        ang = np.degrees(np.arctan2(dx, -dy))                     # 0° = haut, sens horaire
        diff = np.abs((ang - head + 180.0) % 360.0 - 180.0)
        m = (dist <= rng) & ((diff <= fov / 2.0) if fov < 359.5 else True)
        m = m | ((np.abs(gx - c.x * w) <= w / cols) & (np.abs(gy - c.y * h) <= h / rows))   # la cellule de la caméra
        masks[c.id] = m
        count += m
    total = rows * cols
    covered = int((count >= 1).sum())
    multi = int((count >= 2).sum())
    per = []
    for c in cams:
        m = masks[c.id]
        alone = int((m & (count == 1)).sum())
        per.append({"camera_id": c.id, "label": c.label, "covered_pct": round(100.0 * m.sum() / total, 1),
                    "only_pct": round(100.0 * alone / total, 1)})
    grid = ["".join(str(min(int(v), 2)) for v in row) for row in count]
    return {"floor_id": floor_id, "cameras_counted": len(cams), "cameras_disabled": ex,
            "covered_pct": round(100.0 * covered / total, 1), "overlap_pct": round(100.0 * multi / total, 1),
            "blind_pct": round(100.0 * (total - covered) / total, 1), "cols": cols, "rows": rows,
            "grid": grid, "cameras": per, "walls_ignored": True}


def get(db, floor_id: int) -> Floor:
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise FloorNotFound()
    return floor


def create(db, name: Any, level: Any, data: bytes) -> Floor:
    name = clean_name(name)
    level = clean_level(level)
    if db.query(Floor).count() >= MAX_FLOORS:
        raise FloorError(f"Nombre maximal d'étages atteint ({MAX_FLOORS}).", status=409)
    img = prepare_image(data)
    floor = Floor(name=name, level=level, **img)
    db.add(floor)
    db.commit()
    db.refresh(floor)
    return floor


def update(db, floor_id: int, fields: Dict[str, Any], data: Optional[bytes] = None) -> Tuple[Floor, bool]:
    """-> (étage, proportions_changées). Si les proportions du plan changent, les positions des caméras sont déformées."""
    floor = get(db, floor_id)
    ratio_changed = False
    if "name" in fields:
        floor.name = clean_name(fields["name"])
    if "level" in fields:
        floor.level = clean_level(fields["level"])
    if data is not None:
        old_w, old_h = floor.width, floor.height
        img = prepare_image(data)
        floor.mime, floor.image, floor.width, floor.height = img["mime"], img["image"], img["width"], img["height"]
        # Les positions sont des fractions du plan : un plan de proportions différentes les déformerait.
        ratio_changed = abs(old_w / old_h - img["width"] / img["height"]) > 0.02
    db.commit()
    db.refresh(floor)
    return floor, ratio_changed


def delete(db, floor_id: int) -> int:
    """Supprime l'étage ; ses caméras redeviennent « non placées » (sans position). Retourne leur nombre."""
    floor = get(db, floor_id)
    n = db.query(Camera).filter(Camera.floor_id == floor_id).update(
        {Camera.floor_id: None, Camera.x: None, Camera.y: None, Camera.heading: None,
         Camera.fov: None, Camera.view_range: None}, synchronize_session=False)
    db.delete(floor)
    db.commit()
    return n
