"""Boîte de réception interne des autorités.

Second petit serveur, distinct de l'application principale : il ne sert que les pages de réception et ne donne accès à rien d'autre
(ni caméras, ni personnes, ni réglages). Chaque autorité a un lien secret qui lui est propre ; elle ne voit que ses messages.

Par défaut le serveur n'écoute que sur cet ordinateur (127.0.0.1). Pour l'ouvrir aux appareils du réseau local, lancer avec
NIIFYEEN_INBOX_HOST=0.0.0.0. La connexion est en HTTP simple : à utiliser sur un réseau isolé ou de confiance.
"""
import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse

from . import config, dispatch
from .db import Authority, SessionLocal

log = logging.getLogger("niifyeen.inbox")
STATIC = Path(__file__).resolve().parent / "static"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow",
           "X-Content-Type-Options": "nosniff", "Content-Security-Policy":
           "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"}


def _authority_by_token(db, token: str) -> Authority:
    if not token or len(token) > 100:
        raise HTTPException(404)
    row = db.query(Authority).filter(Authority.inbox_token == token).first()
    if row is None or not row.enabled:
        raise HTTPException(404)               # même réponse pour « inconnu » et « désactivé » : rien à deviner
    return row


def build_app(get_meta: Optional[Callable[[], Dict[str, Any]]] = None) -> FastAPI:
    app = FastAPI(title="NiifYéén — Réception des autorités", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def secure_headers(request, call_next):
        response = await call_next(request)
        for key, value in HEADERS.items():
            response.headers[key] = value
        return response

    @app.get("/static/inbox.js")
    def script():
        return Response((STATIC / "inbox.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/static/inbox.css")
    def style():
        return Response((STATIC / "inbox.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/a/{token}", response_class=HTMLResponse)
    def page(token: str):
        db = SessionLocal()
        try:
            _authority_by_token(db, token)
        finally:
            db.close()
        return (STATIC / "inbox.html").read_text(encoding="utf-8")

    @app.get("/a/{token}/api/messages")
    def messages(token: str):
        db = SessionLocal()
        try:
            a = _authority_by_token(db, token)
            meta = (get_meta() if get_meta else None) or {}
            msgs = dispatch.inbox_messages(db, a.id)
            for m in msgs:
                cam = meta.get(m["camera_id"]) or {}
                m["camera_label"] = cam.get("label") or m["camera_id"] or ""
                m["camera_place"] = cam.get("location") or ""
            return {"authority": a.name, "service": a.service or "", "messages": msgs}
        finally:
            db.close()

    @app.get("/a/{token}/api/photo/{dispatch_id}/{index}")
    def photo(token: str, dispatch_id: int, index: int):
        db = SessionLocal()
        try:
            a = _authority_by_token(db, token)
            row = db.get(dispatch.Dispatch, dispatch_id)
            if row is None or row.authority_id != a.id:
                raise HTTPException(404)
        finally:
            db.close()
        photos = dispatch.PHOTOS.get_all(dispatch_id)
        if not 0 <= index < len(photos):
            raise HTTPException(404)           # photo expirée (gardée en mémoire seulement) ou absente
        return Response(photos[index], media_type="image/jpeg")

    @app.post("/a/{token}/api/ack/{dispatch_id}")
    def ack(token: str, dispatch_id: int):
        db = SessionLocal()
        try:
            a = _authority_by_token(db, token)
            if not dispatch.acknowledge(db, a.id, dispatch_id):
                raise HTTPException(404)
            return {"ok": True}
        finally:
            db.close()

    @app.delete("/a/{token}/api/messages/{dispatch_id}")
    def delete_one(token: str, dispatch_id: int):
        db = SessionLocal()
        try:
            a = _authority_by_token(db, token)
            if not dispatch.hide_message(db, a.id, dispatch_id):
                raise HTTPException(404)
            return {"ok": True}
        finally:
            db.close()

    @app.delete("/a/{token}/api/messages")
    def delete_all(token: str):
        db = SessionLocal()
        try:
            a = _authority_by_token(db, token)
            return {"ok": True, "deleted": dispatch.hide_all(db, a.id)}
        finally:
            db.close()

    return app


def lan_address() -> Optional[str]:
    """Adresse de cet ordinateur sur le réseau local (sans rien envoyer), pour construire le lien à donner aux autorités."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def _useful_lan(ip: str) -> bool:
    """Écarte les adresses inutiles pour un téléphone : boucle locale, auto-attribuées, cartes virtuelles VirtualBox."""
    return not (ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("192.168.56.") or ip == "0.0.0.0")


def lan_addresses() -> list:
    """Adresses de cet ordinateur sur le réseau local, la plus probable d'abord (utile quand il a plusieurs cartes réseau :
    Wi-Fi, VirtualBox, WSL…)."""
    found = []
    primary = lan_address()
    if primary:
        found.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    good = [ip for ip in found if _useful_lan(ip)]
    # WSL / Hyper-V utilisent souvent 172.16-31.x : on ne les garde qu'en dernier recours
    def wsl_like(ip):
        parts = ip.split(".")
        return parts[0] == "172" and 16 <= int(parts[1]) <= 31
    good.sort(key=wsl_like)
    return good


_LAN_ON = False        # choix fait dans la page « Autorités » (réglage enregistré), en plus de NIIFYEEN_INBOX_HOST


def set_lan(value: bool) -> None:
    global _LAN_ON
    _LAN_ON = bool(value)


def current_host() -> str:
    """Adresse d'écoute : celle de la variable d'environnement, ou 0.0.0.0 si l'ouverture au réseau local est activée."""
    return "0.0.0.0" if _LAN_ON else config.INBOX_HOST


def lan_open() -> bool:
    return current_host() not in ("127.0.0.1", "localhost", "::1")


def public_base() -> str:
    host = current_host()
    if host in ("0.0.0.0", "::", ""):
        host = (lan_addresses() or [None])[0] or lan_address() or "127.0.0.1"
    return f"http://{host}:{config.INBOX_PORT}"


class InboxServer:
    """Lance le serveur de réception dans un thread de l'application."""

    def __init__(self, get_meta: Optional[Callable[[], Dict[str, Any]]] = None):
        self._get_meta = get_meta
        self._server = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if not config.INBOX_ENABLED:
            return False
        import uvicorn
        conf = uvicorn.Config(build_app(self._get_meta), host=current_host(), port=config.INBOX_PORT, log_level="warning",
                              access_log=False, server_header=False)
        self._server = uvicorn.Server(conf)
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._run, name="authority-inbox", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        try:
            self._server.run()
        except (OSError, SystemExit) as exc:
            log.warning("Boîte de réception interne non démarrée (port %s occupé ?) : %s", config.INBOX_PORT, exc)

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def restart(self) -> bool:
        """Relance l'écoute (après un changement d'adresse d'écoute)."""
        self.stop()
        return self.start()
