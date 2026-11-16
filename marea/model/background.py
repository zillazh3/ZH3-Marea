"""Fondo del clip (marea.background v1): portada, video propio o transparente.

Sin dependencias de Qt ni de Skia para que `ExportJob` cruce `multiprocessing` (`spawn`) sin problemas. El mapeo
tiempo del clip -> tiempo del video es una función pura: preview y export piden exactamente el mismo cuadro.
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Callable

from marea.engine.curves import clamp
from marea.model.cinematic import VideoResource

SCHEMA = "marea.background"
VERSION = 1

SOURCES = ("cover", "video", "transparent")
LOOPS = ("repeat", "pingpong", "hold")
FITS = ("cover", "contain")


class BackgroundError(ValueError):
    pass


@dataclass
class BackgroundConfig:
    source: str = "cover"  # cover | video | transparent
    # --- video propio (la ruta, tamaño y mtime nunca viajan en una Corriente) ---
    video: VideoResource | None = None
    video_size: int = 0
    video_mtime_ns: int = 0
    trim_in_ms: float = 0.0
    trim_out_ms: float = 0.0  # 0 = hasta el final del video
    offset_ms: float = 0.0  # >0 = el video ya va adelantado ese tiempo al empezar el clip
    loop: str = "repeat"
    fit: str = "cover"
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    # --- tratamiento ---
    dim: float = 0.25
    blur: float = 0.0
    saturation: float = 1.0
    scrim: float = 0.0  # degradado de lectura tras el bloque de letra
    cover_tint: float = 0.0  # cuánto se ve el fondo de portada difuminado por encima del video

    def clamped(self) -> "BackgroundConfig":
        c = BackgroundConfig(**{f.name: getattr(self, f.name) for f in fields(self)})
        c.source = c.source if c.source in SOURCES else "cover"
        c.loop = c.loop if c.loop in LOOPS else "repeat"
        c.fit = c.fit if c.fit in FITS else "cover"
        c.trim_in_ms = max(0.0, float(c.trim_in_ms))
        c.trim_out_ms = max(0.0, float(c.trim_out_ms))
        c.offset_ms = float(clamp(c.offset_ms, -3_600_000.0, 3_600_000.0))
        c.zoom = float(clamp(c.zoom, 1.0, 3.0))
        c.pan_x, c.pan_y = float(clamp(c.pan_x, -1.0, 1.0)), float(clamp(c.pan_y, -1.0, 1.0))
        c.dim, c.blur = float(clamp(c.dim, 0.0, 0.85)), float(clamp(c.blur, 0.0, 1.0))
        c.saturation, c.scrim = float(clamp(c.saturation, 0.0, 1.6)), float(clamp(c.scrim, 0.0, 1.0))
        c.cover_tint = float(clamp(c.cover_tint, 0.0, 1.0))
        c.video_size, c.video_mtime_ns = max(0, int(c.video_size)), max(0, int(c.video_mtime_ns))
        if c.source == "video" and (c.video is None or not c.video.path):
            c.source = "cover"
        return c

    @property
    def transparent(self) -> bool:
        return self.source == "transparent"

    def window_ms(self) -> tuple[float, float]:
        """(entrada, salida) efectivas del video en ms; salida > entrada siempre que haya video."""
        if self.video is None:
            return 0.0, 0.0
        total = float(max(1, self.video.duration_ms))
        a = min(self.trim_in_ms, max(0.0, total - self.video.frame_duration_ms))
        b = self.trim_out_ms if self.trim_out_ms > a else total
        return a, min(total, max(b, a + self.video.frame_duration_ms))


def source_ms(cfg: BackgroundConfig, t_ms: float, clip_start_ms: float = 0.0) -> float:
    """Tiempo (ms) dentro del video fuente que corresponde a `t_ms` de la canción. Pura y determinista.

    u = tiempo desde el inicio del clip + desplazamiento; luego recorte y modo de repetición. El último cuadro
    de `hold` es el último cuadro entero dentro de la ventana, nunca uno inexistente."""
    a, b = cfg.window_ms()
    span = b - a
    if span <= 0:
        return 0.0
    u = (t_ms - clip_start_ms) + cfg.offset_ms
    frame = cfg.video.frame_duration_ms if cfg.video else 0.0
    if cfg.loop == "hold":
        m = clamp(u, 0.0, max(0.0, span - frame))
    elif cfg.loop == "pingpong":
        p = u % (2.0 * span)
        m = p if p <= span else 2.0 * span - p
        m = min(m, max(0.0, span - frame))
    else:
        m = u % span
    return a + m


def check_video(cfg: BackgroundConfig) -> tuple[BackgroundConfig, str | None]:
    """Si el video falta o cambió desde que se eligió, vuelve a Portada y explica por qué (aviso no bloqueante)."""
    if cfg.source != "video":
        return cfg, None
    path = cfg.video.path if cfg.video else ""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        reason = "no encuentro el archivo"
    else:
        if (cfg.video_size and st.st_size != cfg.video_size) or (cfg.video_mtime_ns and st.st_mtime_ns != cfg.video_mtime_ns):
            reason = "el archivo cambió"
        else:
            return cfg, None
    cfg = BackgroundConfig(**{f.name: getattr(cfg, f.name) for f in fields(cfg)})
    cfg.source = "cover"
    return cfg, f"El video de fondo no está disponible ({reason}). Usé la portada; vuelve a elegir el video en Fondo."


MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


def to_dict(cfg: BackgroundConfig) -> dict:
    return {"schema": SCHEMA, "version": VERSION, **asdict(cfg)}  # asdict recurses into VideoResource


def migrate_dict(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise BackgroundError("La configuración de fondo no es un objeto JSON válido.")
    if raw.get("schema", SCHEMA) != SCHEMA:
        raise BackgroundError(f"Esquema de fondo no soportado: {raw.get('schema')!r}.")
    try:
        ver = int(raw.get("version", 1))
    except (TypeError, ValueError):
        raise BackgroundError("Versión de fondo no válida.") from None
    if ver < 1 or ver > VERSION:
        raise BackgroundError(f"Versión de fondo no soportada: v{ver} (esta app lee hasta v{VERSION}).")
    cur = dict(raw)
    while ver < VERSION:
        cur = MIGRATIONS[ver](cur)
        ver = int(cur.get("version", ver + 1))
    return cur


def _num(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def from_dict(raw: dict | None) -> BackgroundConfig:
    if not raw:
        return BackgroundConfig()
    d = migrate_dict(raw)
    names = {f.name for f in fields(BackgroundConfig)}
    base = BackgroundConfig()
    kw: dict = {}
    for k, v in d.items():
        if k not in names or k == "video":
            continue
        dv = getattr(base, k)
        if isinstance(dv, str):
            kw[k] = str(v)
        elif isinstance(dv, int):  # no pasar por float: un mtime en ns pierde precisión
            try:
                kw[k] = int(v)
            except (TypeError, ValueError, OverflowError):
                pass
        elif isinstance(dv, float):
            kw[k] = _num(v, dv)
    vd = d.get("video")
    if isinstance(vd, dict) and vd.get("path"):
        vnames = {f.name for f in fields(VideoResource)}
        vk = {k: v for k, v in vd.items() if k in vnames}
        vk.setdefault("id", "bg_video")
        try:
            kw["video"] = VideoResource(**vk)
        except TypeError:
            pass
    return BackgroundConfig(**kw).clamped()
