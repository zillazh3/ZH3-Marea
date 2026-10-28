"""Trabajo de export: rango del clip, nombre de archivo, render determinista de frames y codificación."""
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import skia

from marea.engine.engine import Params
from marea.export.ffmpeg import FfmpegWriter
from marea.model.lyrics import Lyrics
from marea.render.gpu import make_context
from marea.render.scene import Scene, Style

PRESETS = {"tiktok": ("TikTok 9:16 · 1080×1920", 1080, 1920), "square": ("Cuadrado 1:1 · 1080×1080", 1080, 1080),
           "wide": ("Horizontal 16:9 · 1920×1080", 1920, 1080)}
TAIL_MS = 500  # el clip dura un poco más que el último verso
PREROLL_MS = 3000  # los springs se simulan 3 s antes del primer frame para llegar asentados


class ExportCancelled(Exception):
    pass


@dataclass
class ExportJob:
    lyrics: Lyrics
    cover_png: bytes
    out_path: Path
    start_ms: float
    end_ms: float
    size: tuple[int, int] = (1080, 1920)
    fps: int = 60
    encoder: str = "cpu"  # "cpu" (libx264) | "nvenc"
    marker: str = "none"  # "none" | "flash" (primer frame blanco) | "beep" (pitido de 120 ms)
    params: Params = field(default_factory=Params)
    style: Style = field(default_factory=Style)
    prefer_gpu: bool = True
    audio_path: Path | None = None  # WAV ya recortado al tramo (con fundidos); el pitido solo aplica sin audio

    @property
    def frames(self) -> int:
        return max(1, round((self.end_ms - self.start_ms) / 1000 * self.fps))


def clip_range(lyrics: Lyrics, first: int, last: int, tail_ms: float = TAIL_MS) -> tuple[float, float]:
    """(inicio, fin) en ms de los versos first..last (índices de 0 y ambos incluidos); tail_ms = outro tras el último verso."""
    chosen = lyrics.lines[first:last + 1]
    return chosen[0].start, max(l.end for l in chosen) + tail_ms


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clip_name(artist: str, title: str, first: int | None = None, last: int | None = None) -> str:
    """`Artista - Canción (versos X–Y) [ZH3 Marea].mp4`; first/last son índices de 0. Sin ellos: `(completa)`."""
    base = f"{artist.strip()} - {title.strip()}" if artist.strip() else title.strip() or "Sin título"
    base = _INVALID.sub("", base).strip(" .")[:100] or "Sin título"
    span = "completa" if first is None else f"versos {first + 1}–{last + 1}"
    return f"{base} ({span}) [ZH3 Marea].mp4"


def unique_path(folder: Path, name: str) -> Path:
    p, stem, suffix, n = folder / name, Path(name).stem, Path(name).suffix, 2
    while p.exists():
        p = folder / f"{stem} ({n}){suffix}"
        n += 1
    return p


def render_frames(job: ExportJob, ctx=None) -> Iterator[np.ndarray]:
    """Frames RGBA (alto, ancho, 4) del clip. Paso fijo de 1/fps: mismo trabajo => mismos píxeles."""
    own = ctx is None
    ctx = ctx or make_context(job.prefer_gpu)
    try:
        cover = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(job.cover_png))
        scene = Scene(job.lyrics, cover, job.size, job.params, Style(**vars(job.style)), gpu=ctx.gpu)
        scene.clip_start_ms = job.start_ms
        surface = ctx.make_surface(*job.size)
        canvas = surface.getCanvas()
        dt = 1 / job.fps
        scene.engine.warmup(job.start_ms, dt, PREROLL_MS)
        for i in range(job.frames):
            if i:
                scene.engine.advance(dt)
            scene.render(canvas)
            frame = ctx.read_rgba(surface)
            if i == 0 and job.marker == "flash":
                frame = np.full_like(frame, 255)
            yield frame
    finally:
        if own:
            ctx.close()


def run_export(job: ExportJob, progress: Callable[[int, int, float], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> Path:
    """Renderiza y codifica. progress(hechos, total, segundos restantes); cancelled() -> bool."""
    ctx = make_context(job.prefer_gpu)
    t0, total, last_report = time.perf_counter(), job.frames, 0.0
    try:
        with FfmpegWriter(job.out_path, *job.size, job.fps, job.encoder, beep=job.marker == "beep" and not job.audio_path,
                          audio=job.audio_path) as out:
            for i, frame in enumerate(render_frames(job, ctx)):
                if cancelled and cancelled():
                    raise ExportCancelled()
                out.write(frame)
                now = time.perf_counter()
                if progress and (now - last_report > 0.25 or i + 1 == total):
                    last_report = now
                    progress(i + 1, total, (now - t0) / (i + 1) * (total - i - 1))
    finally:
        ctx.close()
    return job.out_path


def worker_main(job: ExportJob, queue, cancel):
    """Punto de entrada del proceso de render (multiprocessing, spawn)."""
    try:
        path = run_export(job, lambda d, n, eta: queue.put(("progress", d, n, eta)), cancel.is_set)
        queue.put(("done", str(path)))
    except ExportCancelled:
        queue.put(("cancelled",))
    except Exception as e:  # el mensaje llega a la UI; el detalle técnico va en `detail`
        import traceback
        queue.put(("error", str(e), traceback.format_exc()))
