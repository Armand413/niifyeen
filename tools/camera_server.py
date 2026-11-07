#!/usr/bin/env python3
"""Diffuse la webcam de CET ordinateur sur le réseau, pour NiifYéén.

Fichier autonome : à copier sur chaque ordinateur dont on veut utiliser la caméra.
Seule dépendance :  pip install opencv-python

    python camera_server.py                      # webcam 0, port 8081
    python camera_server.py --camera 1 --port 8082
    python camera_server.py --token MOT_DE_PASSE # exige ?token=MOT_DE_PASSE

Dans cameras.json du serveur NiifYéén :
    { "id": "salle", "source": "http://ADRESSE_IP_DE_CET_ORDINATEUR:8081/video" }

Adresses :  /video (flux MJPEG)  ·  /snapshot.jpg (une image)  ·  / (aperçu)
ATTENTION : le flux n'est pas chiffré. À utiliser sur un réseau de confiance uniquement.
"""
import argparse
import hmac
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

BOUNDARY = "frame"
PAGE = """<!doctype html><meta charset="utf-8"><title>Caméra NiifYéén</title>
<body style="font-family:system-ui,sans-serif;margin:24px">
<h1>Caméra NiifYéén</h1>
<p>Le flux est disponible à l'adresse <code>/video</code>{token_hint}. Aperçu :</p>
<img src="/video{query}" style="max-width:100%;border:1px solid #ccc" alt="Aperçu de la caméra">
</body>"""


class FrameSource:
    """Lit la caméra dans un thread et garde la dernière image JPEG, partagée par tous les clients."""

    def __init__(self, camera=0, width=640, height=480, fps=15, quality=70, test_pattern=False):
        self.camera, self.width, self.height = camera, width, height
        self.fps, self.quality, self.test_pattern = fps, quality, test_pattern
        self._cond = threading.Condition()
        self._jpeg = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.error = None

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _open(self):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera, backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def _synthetic_frame(self, n):
        img = np.full((self.height, self.width, 3), 235, dtype=np.uint8)
        x = (n * 6) % max(1, self.width - 60)
        cv2.rectangle(img, (x, self.height // 2 - 30), (x + 60, self.height // 2 + 30), (30, 120, 220), -1)
        cv2.putText(img, f"IMAGE DE TEST {n}", (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (60, 60, 60), 2)
        return img

    def _publish(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if ok:
            with self._cond:
                self._jpeg = buf.tobytes()
                self._seq += 1
                self._cond.notify_all()

    def _run(self):
        period = 1.0 / max(1, self.fps)
        cap, n, failures = None, 0, 0
        while not self._stop.is_set():
            started = time.time()
            if self.test_pattern:
                self._publish(self._synthetic_frame(n))
                n += 1
            else:
                if cap is None:
                    cap = self._open()
                    if not cap.isOpened():
                        self.error = f"Impossible d'ouvrir la caméra {self.camera} (occupée ou absente ?)"
                        cap.release(); cap = None
                        self._stop.wait(2.0)
                        continue
                    self.error = None
                ok, frame = cap.read()
                if ok and frame is not None:
                    failures = 0
                    self._publish(frame)
                else:
                    failures += 1
                    if failures > 20:                       # caméra débranchée : on la rouvre
                        cap.release(); cap, failures = None, 0
            self._stop.wait(max(0.0, period - (time.time() - started)))
        if cap is not None:
            cap.release()

    def latest(self):
        with self._cond:
            return self._jpeg

    def wait_next(self, last_seq, timeout=5.0):
        """Attend une image plus récente que ``last_seq`` -> (seq, jpeg) ou (last_seq, None) au bout du délai."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq or self._stop.is_set(), timeout)
            if self._seq == last_seq or self._jpeg is None:
                return last_seq, None
            return self._seq, self._jpeg


def make_handler(source: FrameSource, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "NiifYeenCamera"

        def log_message(self, fmt, *args):      # silencieux : un flux vidéo remplirait la console
            pass

        def _authorized(self, query):
            if not token:
                return True
            given = (query.get("token") or [""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._authorized(query):
                return self._send(401, "Jeton manquant ou incorrect (ajouter ?token=...).".encode())
            if url.path == "/":
                suffix = f"?token={query['token'][0]}" if token else ""
                hint = " (avec <code>?token=…</code>)" if token else ""
                return self._send(200, PAGE.format(query=suffix, token_hint=hint).encode(), "text/html; charset=utf-8")
            if url.path == "/snapshot.jpg":
                jpeg = source.latest()
                if jpeg is None:
                    return self._send(503, (source.error or "Pas encore d'image.").encode())
                return self._send(200, jpeg, "image/jpeg")
            if url.path == "/video":
                return self._stream()
            self._send(404, "Introuvable. Essayez /video".encode())

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = -1
            try:
                while True:
                    seq, jpeg = source.wait_next(seq, timeout=5.0)
                    if jpeg is None:
                        if source._stop.is_set():
                            return
                        continue
                    self.wfile.write(f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                     f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return                                       # le client a fermé la connexion

    return Handler


def local_addresses():
    """Adresses IPv4 probables de cet ordinateur sur le réseau local."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))       # n'envoie rien : sert à choisir l'interface réseau
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [a for a in dict.fromkeys(found) if not a.startswith("127.")]


def build_server(source, host="0.0.0.0", port=8081, token=""):
    server = ThreadingHTTPServer((host, port), make_handler(source, token))
    server.daemon_threads = True
    return server


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diffuse la webcam de cet ordinateur sur le réseau (flux MJPEG).")
    ap.add_argument("--camera", type=int, default=0, help="index de la webcam (0 = la première)")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = accessible depuis le réseau ; 127.0.0.1 = cet ordinateur seulement")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", type=int, default=70, help="qualité JPEG, 1-100 (plus bas = moins de débit)")
    ap.add_argument("--token", default="", help="mot de passe à fournir dans l'URL (?token=...)")
    ap.add_argument("--test-pattern", action="store_true", help="image de test, sans webcam")
    args = ap.parse_args(argv)

    source = FrameSource(args.camera, args.width, args.height, args.fps, args.quality, args.test_pattern).start()
    try:
        server = build_server(source, args.host, args.port, args.token)
    except OSError as exc:
        print(f"Impossible d'utiliser le port {args.port} : {exc}\nEssayez --port {args.port + 1}.")
        source.stop()
        return 1

    query = f"?token={args.token}" if args.token else ""
    print("Caméra en cours de diffusion. Dans cameras.json du serveur NiifYéén, utilisez l'une de ces sources :")
    for ip in (local_addresses() if args.host == "0.0.0.0" else [args.host]):
        print(f'    "source": "http://{ip}:{args.port}/video{query}"')
    print("Aperçu dans un navigateur : même adresse, sans /video.   Arrêt : Ctrl + C")
    if args.token == "":
        print("Avertissement : aucun --token ; toute personne du réseau peut voir cette caméra.")
    time.sleep(1.0)
    if source.error:
        print("Avertissement :", source.error)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
        source.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
