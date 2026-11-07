"""Lance NiifYéén en mode démonstration : caméras et personnes simulées, base séparée (niifyeen-demo.db).

    .\\venv\\Scripts\\python.exe tools\\run_demo.py [--port 8000] [--speed 1]

Le serveur écoute sur 127.0.0.1 uniquement. Aucune donnée réelle n'est lue ni écrite.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--speed", type=float, default=1.0, help="vitesse du scénario (1 = temps réel, 4 = quatre fois plus vite)")
    args = parser.parse_args()
    os.environ["NIIFYEEN_DEMO"] = "1"
    os.environ["NIIFYEEN_DEMO_SPEED"] = str(args.speed)
    # Aucune notification externe, même si des variables d'environnement existent.
    for name in ("NIIFYEEN_SMTP_HOST", "NIIFYEEN_TELEGRAM_TOKEN"):
        os.environ.pop(name, None)
    import uvicorn
    print(f"Démonstration : http://127.0.0.1:{args.port}/demo  (Ctrl+C pour arrêter)")
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
