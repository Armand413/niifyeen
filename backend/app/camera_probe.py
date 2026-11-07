"""Test de connexion, détection des webcams et découverte de caméras ONVIF.

Tout est exécuté à la demande de l'opérateur. Les sources testées sont celles que l'opérateur saisit :
sans authentification sur l'application, n'importe qui pouvant l'atteindre pourrait faire sonder le
réseau local depuis le serveur (voir le README : rester sur 127.0.0.1).
"""
import base64
import logging
import os
import re
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urlsplit

import cv2

from . import config

log = logging.getLogger(__name__)

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)
PREVIEW_MAX_WIDTH = 480


def _backends(source: Union[int, str]) -> List[int]:
    if isinstance(source, str):
        return [cv2.CAP_FFMPEG, cv2.CAP_ANY]
    return [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if os.name == "nt" else [cv2.CAP_ANY]


def open_capture(source: Union[int, str], timeout_sec: float = config.PROBE_TIMEOUT_SEC):
    """Ouvre la source avec des délais maximaux (si OpenCV les gère) ; None si impossible."""
    timeout_ms = int(timeout_sec * 1000)
    for backend in _backends(source):
        cap = None
        try:
            try:
                cap = cv2.VideoCapture(source, backend, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
                                                         cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms])
            except TypeError:                                   # OpenCV ancien : pas de paramètres de délai
                cap = cv2.VideoCapture(source, backend)
            if cap is not None and cap.isOpened():
                return cap
        except Exception:
            log.debug("ouverture impossible avec le backend %s", backend, exc_info=True)
        if cap is not None:
            cap.release()
    return None


def _hint(kind: str, opened: bool) -> str:
    if opened:
        return "La connexion s'ouvre mais aucune image n'est reçue : la caméra est peut-être occupée ou le flux vide."
    if kind == "webcam":
        return "Webcam introuvable, ou déjà utilisée par un autre programme (visioconférence, autre caméra NiifYéén…)."
    if kind == "rtsp":
        return ("Aucune réponse. Vérifiez l'adresse IP, le port (554 en général), le chemin du flux, l'identifiant et le "
                "mot de passe de la caméra, et que l'ordinateur est sur le même réseau.")
    return ("Aucune réponse à cette adresse. Vérifiez l'adresse IP et le port, le pare-feu de l'autre ordinateur, "
            "le jeton éventuel, et que le programme de diffusion est lancé.")


def preview_data_url(frame, max_width: int = PREVIEW_MAX_WIDTH) -> Optional[str]:
    h, w = frame.shape[:2]
    if w > max_width:
        frame = cv2.resize(frame, (max_width, max(1, int(h * max_width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else None


def probe_source(source: Union[int, str], kind: str, timeout_sec: float = config.PROBE_TIMEOUT_SEC,
                 sample_sec: float = 1.0, capture_size: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """Ouvre la source, lit quelques images et mesure ce qui peut l'être. Ne lève pas d'exception."""
    started = time.time()
    cap = open_capture(source, timeout_sec)
    if cap is None:
        return {"ok": False, "error": _hint(kind, opened=False)}
    try:
        if isinstance(source, int) and capture_size:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, capture_size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, capture_size[1])
        ok, frame = cap.read()
        if not ok or frame is None:
            return {"ok": False, "error": _hint(kind, opened=True)}
        first_frame_ms = round((time.time() - started) * 1000)
        height, width = frame.shape[:2]
        preview = preview_data_url(frame)

        frames, t0 = 0, time.time()
        while time.time() - t0 < sample_sec and frames < 120:
            ok, _ = cap.read()
            if not ok:
                break
            frames += 1
        elapsed = time.time() - t0
        fps = round(frames / elapsed, 1) if frames >= 2 and elapsed > 0 else None
        return {"ok": True, "width": width, "height": height, "fps": fps, "first_frame_ms": first_frame_ms,
                "preview": preview, "kind": kind}
    except Exception as exc:
        log.exception("Test de connexion")
        return {"ok": False, "error": f"Erreur pendant le test : {type(exc).__name__}"}
    finally:
        cap.release()


def list_webcams(skip: Iterable[int] = (), max_index: int = 6) -> List[Dict[str, Any]]:
    """Webcams locales disponibles. Les index ``skip`` (déjà utilisés par NiifYéén) ne sont pas rouverts."""
    skip = set(skip)
    found = []
    for index in range(max_index):
        if index in skip:
            continue
        cap = open_capture(index, timeout_sec=2.0)
        if cap is None:
            continue
        try:
            ok, frame = cap.read()
            if ok and frame is not None:
                found.append({"index": index, "width": frame.shape[1], "height": frame.shape[0]})
        finally:
            cap.release()
    return found


# ---- ONVIF (WS-Discovery) ---------------------------------------------------------------------------
PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    '<e:Header><w:MessageID>uuid:{message_id}</w:MessageID>'
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
    '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')


def build_probe() -> bytes:
    return PROBE_TEMPLATE.format(message_id=uuid.uuid4()).encode("utf-8")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_discovery_response(data: bytes) -> Optional[Dict[str, Any]]:
    """Extrait l'adresse et les indications d'une réponse WS-Discovery ; None si ce n'est pas une caméra ONVIF.

    Les réponses viennent du réseau : tout document contenant une déclaration DOCTYPE ou ENTITY est refusé
    avant analyse (protection contre les attaques par expansion d'entités XML).
    """
    if len(data) > 64 * 1024:
        return None
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    values = {}
    for node in root.iter():
        name = _local(node.tag)
        if name in ("XAddrs", "Scopes", "Types") and node.text:
            values[name] = node.text.strip()
    if "NetworkVideoTransmitter" not in values.get("Types", "") and "onvif" not in values.get("Scopes", "").lower():
        return None
    xaddrs = [x for x in values.get("XAddrs", "").split() if urlsplit(x).scheme in ("http", "https")]
    host = next((urlsplit(x).hostname for x in xaddrs if urlsplit(x).hostname), None)
    if not host:
        return None
    scopes = values.get("Scopes", "").split()

    def scope(key: str) -> str:
        for item in scopes:
            m = re.search(r"/" + key + r"/([^/\s]+)", item)
            if m:
                return re.sub(r"%20", " ", m.group(1))[:60]
        return ""

    return {"address": host, "xaddrs": xaddrs[:3], "name": scope("name"), "hardware": scope("hardware"),
            "suggested_source": f"rtsp://{host}:554/"}


def discover_onvif(timeout_sec: float = 2.5, sock_factory=socket.socket) -> List[Dict[str, Any]]:
    """Cherche les caméras ONVIF du réseau local (WS-Discovery, UDP multicast). Ne récupère pas l'adresse du flux."""
    found: Dict[str, Dict[str, Any]] = {}
    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        sock.sendto(build_probe(), WS_DISCOVERY_ADDR)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            device = parse_discovery_response(data)
            if device and device["address"] not in found:
                found[device["address"]] = device
    except OSError as exc:
        log.warning("Découverte ONVIF impossible : %s", exc)
    finally:
        sock.close()
    return list(found.values())
