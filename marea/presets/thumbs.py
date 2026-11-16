"""Miniaturas de las Corrientes: la canción demo dibujada por el motor real (CPU) en un instante fijo, con caché en disco."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from marea.engine.engine import Params
from marea.model.background import BackgroundConfig
from marea.presets.schema import FxSettings, apply
from marea.presets.store import PresetInfo, user_dir

THUMB_SIZE = (216, 384)
THUMB_T_MS = 12500  # un verso a media canción, con el motor ya asentado


def thumb_path(info: PresetInfo, folder: Path | None = None) -> Path:
    """La clave es el hash del look: al cambiar la Corriente se genera otra miniatura sin invalidar nada a mano."""
    return (folder or user_dir() / "thumbs") / f"{info.signature}.png"


def render_thumb(info: PresetInfo, out: Path) -> Path:
    import skia

    from marea.demo_cover import demo_cover
    from marea.demo_song import exact_lyrics
    from marea.render.scene import Scene, Style

    style, params, bg, _ = apply(info.preset, Style(title="Marea de luz", artist="Demo", wm_text=""), Params(),
                                 BackgroundConfig(), FxSettings())
    bg = replace(bg, source="cover")  # una Corriente nunca trae el archivo de video: la miniatura usa la portada demo
    scene = Scene(exact_lyrics(), demo_cover(), THUMB_SIZE, params, style, gpu=False, background=bg)
    try:
        data = scene.snapshot(THUMB_T_MS).encodeToData(skia.EncodedImageFormat.kPNG)
    finally:
        scene.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(data))
    return out


def ensure_thumb(info: PresetInfo, folder: Path | None = None) -> Path:
    p = thumb_path(info, folder)
    return p if p.is_file() else render_thumb(info, p)
