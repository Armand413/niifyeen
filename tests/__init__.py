"""Prépare l'environnement de test *avant* tout import de ``backend.app``.

- la base SQLite est un fichier temporaire (jamais ``niifyeen.db``) ;
- ``ultralytics`` est remplacé par un faux module : les tests ne chargent pas YOLO.

Lancer les tests avec :  python -m unittest discover -s tests -t . -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="niifyeen-tests-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

os.environ["NIIFYEEN_DB"] = os.path.join(_TMP, "test.db")
os.environ["NIIFYEEN_CAMERAS_FILE"] = os.path.join(_TMP, "cameras.json")

if "ultralytics" not in sys.modules:
    fake = types.ModuleType("ultralytics")
    fake.YOLO = object
    sys.modules["ultralytics"] = fake
