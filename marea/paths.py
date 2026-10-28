import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSettings


def _portable_dir() -> Path | None:
    """Junto al .exe empaquetado (`data/`), si se puede escribir ahí: la app entera cabe en una carpeta que se mueve sola."""
    if not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).parent / "data"
    try:
        p.mkdir(exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("")
        probe.unlink()
    except OSError:
        return None  # carpeta de solo lectura (p. ej. Program Files): se usa LOCALAPPDATA
    return p


def app_data() -> Path:
    """Carpeta de datos de la app (caché de letras, ajustes, registro), creada si no existe."""
    p = _portable_dir()
    if p is None:
        base = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
        p = Path(base) / "ZH3 Marea"
        p.mkdir(parents=True, exist_ok=True)
    return p


def default_output_dir() -> Path:
    return Path.home() / "Videos" / "ZH3 Marea"


def make_settings() -> QSettings:
    """Ajustes en un .ini dentro de la carpeta de datos (nada en el registro de Windows)."""
    return QSettings(str(app_data() / "settings.ini"), QSettings.IniFormat)


def reveal_in_folder(path: str):
    """Abre el Explorador de Windows con el archivo seleccionado."""
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
