"""Instalación real de punta a punta (NO es un test de la suite: descarga ~3 GB). Uso:  .venv\\Scripts\\python tests\\_lab_e2e_install.py <carpeta lab>"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marea.lab import env  # noqa: E402

root = Path(sys.argv[1])
t0 = time.perf_counter()
last = {}


def progress(stage, text):
    if stage == "uv" and last.get("uv", 0) + 3_000_000 > int(text.split()[0]):
        return
    if stage == "uv":
        last["uv"] = int(text.split()[0])
    print(f"[{time.perf_counter() - t0:6.1f}s] {stage}: {text}", flush=True)


py = env.install(root, progress, threading.Event())
print(f"OK {py} en {time.perf_counter() - t0:.0f}s; is_installed={env.is_installed(root)}")
