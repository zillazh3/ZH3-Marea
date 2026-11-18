"""Historial de clips exportados (para «clips recientes» en Inicio) y miniaturas de sus videos."""
import json
import subprocess
from datetime import datetime
from pathlib import Path

from marea.paths import app_data
from marea.sysutil import run_ffmpeg

MAX_ENTRIES = 50


def _file() -> Path:
    return app_data() / "history.json"


def load(path: Path | None = None) -> list[dict]:
    """Más recientes primero; descarta los videos que ya no existen."""
    try:
        items = json.loads((path or _file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{**e, "loop": bool(e.get("loop", False))}
            for e in items if isinstance(e, dict) and Path(e.get("path", "")).exists()]


def add(entry: dict, path: Path | None = None) -> list[dict]:
    items = [e for e in load(path) if e["path"] != entry["path"]]
    items.insert(0, {**entry, "date": datetime.now().isoformat(timespec="seconds")})
    items = items[:MAX_ENTRIES]
    try:
        (path or _file()).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # sin historial también funciona
    return items


def make_thumbnail(video: Path, out_dir: Path | None = None, at_s: float = 1.0) -> Path | None:
    """Un frame del video, de 270 px de ancho. None si ffmpeg no pudo (el historial sigue sin miniatura)."""
    out_dir = out_dir or app_data() / "thumbs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (video.stem[:80] + ".png")
    src, seek = video, ["-ss", str(at_s)]
    if video.is_dir():  # secuencia PNG: se toma el cuadro más cercano a `at_s`
        frames = sorted(video.glob("*.png"))
        if not frames:
            return None
        try:
            fps = float(json.loads((video / "marea.json").read_text(encoding="utf-8")).get("fps", 60))
        except (OSError, ValueError, TypeError):
            fps = 60.0
        src, seek = frames[min(len(frames) - 1, round(at_s * fps))], []
    try:
        r = run_ffmpeg(["-y", "-loglevel", "error", *seek, "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=270:-2", str(out)], timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return out if r.returncode == 0 and out.exists() else None
