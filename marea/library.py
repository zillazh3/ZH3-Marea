"""Registro de proyectos guardados o abiertos (para la Biblioteca). Es un índice: nunca toca los .marea.json."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from marea.paths import app_data

MAX_ENTRIES = 200


def _file() -> Path:
    return app_data() / "library.json"


def load(path: Path | None = None) -> list[dict]:
    """Más recientes primero; descarta los proyectos cuyo archivo ya no existe."""
    try:
        items = json.loads((path or _file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in items if isinstance(e, dict) and Path(e.get("path", "")).is_file()]


def _write(items: list[dict], path: Path | None) -> None:
    try:
        (path or _file()).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # sin biblioteca la app también funciona


def add(entry: dict, path: Path | None = None) -> list[dict]:
    items = [e for e in load(path) if e["path"] != entry["path"]]
    items.insert(0, {**entry, "date": datetime.now().isoformat(timespec="seconds")})
    items = items[:MAX_ENTRIES]
    _write(items, path)
    return items


def remove(project_path: str, path: Path | None = None) -> list[dict]:
    items = [e for e in load(path) if e["path"] != project_path]
    _write(items, path)
    return items


def register(project, project_path: str | Path, path: Path | None = None, thumbs: Path | None = None) -> list[dict]:
    """Anota un ClipProject guardado en `project_path`; la miniatura es su portada en PNG."""
    p = Path(project_path).resolve()
    thumb = ""
    try:
        thumbs = thumbs or app_data() / "thumbs"
        thumbs.mkdir(parents=True, exist_ok=True)
        out = thumbs / f"proj-{hashlib.sha1(str(p).encode()).hexdigest()[:16]}.png"
        out.write_bytes(project.cover_png())
        thumb = str(out)
    except (OSError, TypeError):
        pass  # sin miniatura la tarjeta usa el relleno
    return add({"path": str(p), "title": project.title, "artist": project.artist,
                "kind": project.lyrics.kind, "verses": len(project.lyrics.lines), "thumb": thumb}, path)
