"""Trabajo de export: rango del clip, nombre de archivo, render determinista de frames y codificación."""
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import skia

from marea.engine.engine import Params
from marea.export.ffmpeg import FfmpegWriter
from marea.model.cinematic import CinematicConfig
from marea.model.lyrics import Lyrics
from marea.render.gpu import make_context
from marea.render.scene import Scene, Style
from marea.sysutil import safe_filename, unique_path

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
    cinematic: CinematicConfig = field(default_factory=CinematicConfig)
    wordfx: object | None = None  # marea.wordfx.plan.FxPlan (experimental) ya compuesto: el mismo plan que vio la preview

    @property
    def frames(self) -> int:
        return max(1, round((self.end_ms - self.start_ms) / 1000 * self.fps))


def clip_range(lyrics: Lyrics, first: int, last: int, tail_ms: float = TAIL_MS) -> tuple[float, float]:
    """(inicio, fin) en ms de los versos first..last (índices de 0 y ambos incluidos); tail_ms = outro tras el último verso."""
    chosen = lyrics.lines[first:last + 1]
    return chosen[0].start, max(l.end for l in chosen) + tail_ms


def clip_name(artist: str, title: str, first: int | None = None, last: int | None = None) -> str:
    """`Artista - Canción (versos X–Y) [ZH3 Marea].mp4`; first/last son índices de 0. Sin ellos: `(completa)`."""
    base = f"{artist.strip()} - {title.strip()}" if artist.strip() else title.strip() or "Sin título"
    base = safe_filename(base, "Sin título")
    span = "completa" if first is None else f"versos {first + 1}–{last + 1}"
    return f"{base} ({span}) [ZH3 Marea].mp4"


def render_frames(job: ExportJob, ctx=None) -> Iterator[np.ndarray]:
    """Frames RGBA (alto, ancho, 4) del clip. Paso fijo de 1/fps: mismo trabajo => mismos píxeles."""
    own = ctx is None
    ctx = ctx or make_context(job.prefer_gpu)
    scene = None
    try:
        cover = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(job.cover_png))
        scene = Scene(job.lyrics, cover, job.size, job.params, Style(**vars(job.style)), gpu=ctx.gpu,
                      cinematic=job.cinematic, wordfx=job.wordfx)
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
        if scene is not None:
            scene.close()
        if own:
            ctx.close()


def run_export(job: ExportJob, progress: Callable[[int, int, float], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> Path:
    """Renderiza y codifica con pipeline render/escritura en paralelo y ETA móvil."""
    ctx = make_context(job.prefer_gpu)
    total = job.frames
    frame_q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=3)
    writer_err: list[BaseException] = []
    stop_ev = threading.Event()
    recent_times: deque[float] = deque(maxlen=48)
    try:
        with FfmpegWriter(job.out_path, *job.size, job.fps, job.encoder, beep=job.marker == "beep" and not job.audio_path,
                          audio=job.audio_path) as out:
            t0 = time.perf_counter()
            recent_times.append(t0)

            def _writer_loop():
                done_frames = 0
                last_report = 0.0
                try:
                    while True:
                        item = frame_q.get()
                        if item is None:
                            return
                        out.write(item)
                        done_frames += 1
                        now = time.perf_counter()
                        recent_times.append(now)
                        if progress and (now - last_report > 0.25 or done_frames == total):
                            last_report = now
                            n_win = len(recent_times)
                            sec_per_frame = ((now - recent_times[0]) / (n_win - 1)) if n_win >= 4 else ((now - t0) / done_frames)
                            progress(done_frames, total, sec_per_frame * (total - done_frames))
                except BaseException as exc:
                    writer_err.append(exc)
                    stop_ev.set()

            writer = threading.Thread(target=_writer_loop, daemon=True)
            writer.start()
            rendered = False
            try:
                for frame in render_frames(job, ctx):
                    if stop_ev.is_set() or (cancelled and cancelled()):
                        if writer_err:
                            raise writer_err[0]
                        raise ExportCancelled()
                    while not stop_ev.is_set():
                        try:
                            frame_q.put(frame, timeout=0.05)
                            break
                        except queue.Full:
                            if cancelled and cancelled():
                                raise ExportCancelled()
                    if writer_err:
                        raise writer_err[0]
                rendered = True
            finally:
                stop_ev.set()
                if not rendered or writer_err:
                    abort = getattr(out, "abort", None)
                    if abort:
                        abort()
                while True:
                    try:
                        frame_q.put(None, timeout=0.05)
                        break
                    except queue.Full:
                        if rendered and not writer_err:
                            continue  # fin normal: el escritor vacía la cola antes del centinela
                        try:
                            frame_q.get_nowait()
                        except queue.Empty:
                            pass
                writer.join(timeout=None if rendered and not writer_err else 10.0)
            if writer_err:
                raise writer_err[0]
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
