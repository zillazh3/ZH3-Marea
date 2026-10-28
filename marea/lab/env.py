"""Instala (y desinstala) el entorno pesado del sidecar: uv + Python 3.12 + torch cu128 + modelos. Bloqueante: la UI lo corre en un hilo.

Todo vive dentro de UNA carpeta (`lab/`): el binario de uv, el Python gestionado, la caché de uv y el entorno. Borrarla desinstala todo.
`progress(etapa, texto)` solo recibe lo que realmente pasó: bytes descargados, o las líneas que escribe uv.
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from marea.export.ffmpeg import NO_WINDOW
from marea.lab.sidecar import kill_tree, sidecar_root
from marea.paths import app_data

# ponytail: uv fijado por versión + SHA-256 (verificado contra el digest que publica la release el 2026-10-27); subir ambos juntos.
UV_VERSION = "0.13.6"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip"
UV_SHA256 = "a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
PYTHON = "3.12"
REQ_DIR = sidecar_root() / "sidecar"  # en el portable, la copia empaquetada como datos

Progress = Callable[[str, str], None]


class InstallError(Exception):
    """Mensaje listo para mostrar al usuario."""


class InstallCancelled(Exception):
    pass


def lab_dir() -> Path:
    return app_data() / "lab"


def env_python(root: Path) -> Path:
    return root / "env" / "Scripts" / "python.exe"


def requirements_hash(req_dir: Path = REQ_DIR) -> str:
    h = hashlib.sha256()
    for name in ("requirements-torch-cu128.txt", "requirements.txt"):
        h.update((req_dir / name).read_bytes())
    h.update(f"{UV_VERSION}|{PYTHON}|{TORCH_INDEX}".encode())
    return h.hexdigest()[:16]


def env_relocated(root: Path) -> bool:
    """El venv guarda la ruta ABSOLUTA de su Python base en pyvenv.cfg: si la carpeta de la app se movió, esa ruta ya no existe y el entorno
    no arranca. Se detecta aquí para decir «reinstala» en vez de fallar sin explicación."""
    try:
        for line in (root / "env" / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            if line.split("=", 1)[0].strip() == "home":
                return not Path(line.split("=", 1)[1].strip()).exists()
    except OSError:
        return False
    return False


def env_runs(root: Path) -> bool:
    """¿Arranca de verdad el Python del entorno? El `python.exe` de un venv de uv es un trampolín con la ruta del Python real INCRUSTADA en el
    ejecutable: arreglar pyvenv.cfg a mano no basta si la carpeta se movió, hay que ejecutarlo para saberlo (≈ 0.1 s)."""
    try:
        return subprocess.run([str(env_python(root)), "-c", "pass"], capture_output=True, timeout=30, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_installed(root: Path, req_dir: Path = REQ_DIR) -> bool:
    """Instalado y al día con los requisitos de esta versión de la app (el marcador se escribe al final, solo si todo salió bien)."""
    try:
        ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return ready.get("requirements") == requirements_hash(req_dir) and env_python(root).exists() and not env_relocated(root)


def _checked(cancel: threading.Event | None):
    if cancel is not None and cancel.is_set():
        raise InstallCancelled()


def fetch_uv(root: Path, progress: Progress, cancel: threading.Event | None = None, url: str = UV_URL, sha256: str = UV_SHA256) -> Path:
    """Descarga uv.exe, verifica el hash fijado ANTES de extraer nada y extrae solo ese archivo."""
    exe = root / "bin" / "uv.exe"
    if exe.exists():
        return exe
    exe.parent.mkdir(parents=True, exist_ok=True)
    zpath = exe.parent / "uv.zip.part"
    h = hashlib.sha256()
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=30) as r, open(zpath, "wb") as f:
            r.raise_for_status()
            total, done = int(r.headers.get("content-length", 0)), 0
            for chunk in r.iter_bytes(1 << 18):
                _checked(cancel)
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                progress("uv", f"{done} / {total or '?'} bytes")
        if h.hexdigest() != sha256:
            raise InstallError("La descarga de uv no coincide con el hash esperado; no se instaló. Revisa tu conexión o antivirus.")
        with zipfile.ZipFile(zpath) as z:
            exe.write_bytes(z.read("uv.exe"))  # un solo miembro, por nombre exacto: nada de rutas del zip se toca
    except httpx.HTTPError as e:
        raise InstallError(f"No pude descargar uv ({e.__class__.__name__}). Revisa tu conexión.") from e
    except (zipfile.BadZipFile, KeyError) as e:
        raise InstallError("El archivo de uv descargado no es válido.") from e
    finally:
        zpath.unlink(missing_ok=True)
    return exe


def _run(root: Path, stage: str, args: list[str], progress: Progress, cancel: threading.Event | None, check_msg: str):
    """Corre uv mostrando sus líneas tal cual; cancelar mata uv y todo lo que lanzó."""
    env = {**os.environ, "UV_CACHE_DIR": str(root / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(root / "python"),
           "UV_NO_PROGRESS": "1", "NO_COLOR": "1", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW,
                            text=True, encoding="utf-8", errors="replace")
    tail = []
    try:
        for line in proc.stdout:
            tail = (tail + [line.rstrip()])[-8:]
            progress(stage, line.rstrip())
            _checked(cancel)
    except InstallCancelled:
        kill_tree(proc.pid)
        raise
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code != 0:
        raise InstallError(f"{check_msg} (código {code}). Últimas líneas:\n" + "\n".join(tail))


def install(root: Path, progress: Progress, cancel: threading.Event | None = None, req_dir: Path = REQ_DIR) -> Path:
    """Deja `root/env` listo y escribe `ready.json`. Devuelve el Python del entorno. Reintentable: uv reutiliza lo ya descargado."""
    root.mkdir(parents=True, exist_ok=True)
    uv = str(fetch_uv(root, progress, cancel))
    py = env_python(root)
    if py.exists() and (env_relocated(root) or not env_runs(root)):  # carpeta movida o venv roto: se rehace (los paquetes salen de la caché de uv)
        progress("python", "El entorno no arranca (¿se movió la carpeta?): se vuelve a crear.")
        shutil.rmtree(root / "env")
    if not py.exists():
        # Python gestionado por uv (dentro de lab/), no el del sistema: el entorno no depende de nada que el usuario pueda desinstalar
        _run(root, "python", [uv, "venv", str(root / "env"), "--python", PYTHON, "--managed-python"], progress, cancel,
             "No pude crear el entorno de Python")
    base = [uv, "pip", "install", "--python", str(py)]
    _run(root, "torch", base + ["-r", str(req_dir / "requirements-torch-cu128.txt"), "--index-url", TORCH_INDEX], progress, cancel,
         "No pude instalar torch")
    _run(root, "paquetes", base + ["-r", str(req_dir / "requirements.txt")], progress, cancel, "No pude instalar las dependencias")
    _run(root, "verificación", [str(py), "-c", "import torch, ctranslate2, transformers, faster_whisper, numpy"], progress, cancel,
         "El entorno se instaló pero no importa")
    (root / "ready.json").write_text(json.dumps({"requirements": requirements_hash(req_dir), "uv": UV_VERSION}), encoding="utf-8")
    return py


def uninstall(root: Path):
    """Borra TODO el laboratorio (entorno, Python gestionado, caché de uv, modelos y caché de alineación). Solo carpetas llamadas `lab`."""
    if root.name != "lab":
        raise InstallError("Ruta inesperada: no borro nada que no sea la carpeta «lab».")
    shutil.rmtree(root, ignore_errors=False)


# ---------------- componentes opcionales y modelos ----------------

SEPARATION_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
ASR_MODEL = "large-v3-turbo"


def separation_installed(root: Path) -> bool:
    return (root / "env" / "Lib" / "site-packages" / "audio_separator").is_dir()


def install_separation(root: Path, progress: Progress, cancel: threading.Event | None = None, req_dir: Path = REQ_DIR) -> None:
    """Instala encima del entorno la librería de separación de voz (opcional)."""
    uv, py = root / "bin" / "uv.exe", env_python(root)
    if not uv.exists() or not py.exists():
        raise InstallError("Primero instala el entorno de sincronía.")
    _run(root, "separación", [str(uv), "pip", "install", "--python", str(py), "-r", str(req_dir / "requirements-separation.txt")], progress, cancel,
         "No pude instalar la separación de voz")


def hf_model_ready(root: Path, repo: str) -> bool:
    """¿Hay una copia local completa de ese repo de Hugging Face dentro de lab/hf? (solo mira el disco: nunca la red)."""
    snaps = root / "hf" / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots"
    return snaps.is_dir() and any(any(p.suffix in (".bin", ".safetensors") for p in s.iterdir()) for s in snaps.iterdir() if s.is_dir())


def separator_model_ready(root: Path, filename: str = SEPARATION_MODEL) -> bool:
    return (root / "separator-models" / filename).is_file()
