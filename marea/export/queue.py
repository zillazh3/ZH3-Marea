"""Cola de exportaciones: de una en una; cada trabajo puede grabar/preparar su audio y luego renderizar el video."""
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from marea.async_util import run_async
from marea.audio import local
from marea.audio.loop import loop_local
from marea.audio.session import RecordingCancelled, record_song_range
from marea.export import history
from marea.export.job import ExportJob
from marea.export.runner import ExportRunner
from marea.i18n import t
from marea.providers.errors import ProviderError
from marea.sysutil import unique_path

log = logging.getLogger(__name__)

QUEUED, AUDIO, RENDER, DONE, FAILED, CANCELLED = "queued", "audio", "render", "done", "failed", "cancelled"
ACTIVE = (AUDIO, RENDER)


@dataclass
class AudioSpec:
    kind: str  # "spotify" (loopback) | "local" (archivo)
    start_ms: float
    end_ms: float
    track: object = None  # Track (spotify)
    samples: object = None  # audio local ya decodificado
    offset_ms: float = 0.0
    device: str = ""
    fade_out_ms: float = 0.0  # el outro: el volumen baja linealmente durante estos últimos ms


@dataclass(eq=False)
class QueueItem:
    title: str
    artist: str
    cover_png: bytes
    job: ExportJob
    audio: AudioSpec | None = None
    state: str = QUEUED
    stage: str = field(default_factory=lambda: t("export.stage.queued"))
    progress: float = 0.0  # 0..1 de la etapa actual
    eta: float | None = None  # segundos
    error: str = ""
    thumb: Path | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    wav: Path | None = None

    @property
    def out_path(self) -> Path:
        return self.job.out_path

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELLED)


class ExportQueue(QObject):
    changed = Signal()  # cambió la lista o el estado de algún trabajo
    item_changed = Signal(object)  # progreso/etapa de un trabajo
    item_done = Signal(object)  # terminó bien
    item_failed = Signal(object)
    _audio_progress = Signal(object, float, str)

    def __init__(self, services, parent=None, runner_factory=ExportRunner):
        super().__init__(parent)
        self.services = services
        self.items: list[QueueItem] = []
        self._active: QueueItem | None = None
        self._runner = runner_factory(self)
        self._runner.progress.connect(self._on_render_progress)
        self._runner.finished.connect(self._on_render_done)
        self._runner.failed.connect(self._on_render_failed)
        self._runner.cancelled.connect(self._on_render_cancelled)
        self._audio_progress.connect(self._on_audio_progress)

    # ---- API ----
    def add(self, item: QueueItem):
        out = item.job.out_path  # el destino se asigna aquí: los trabajos en cola o en curso aún no tienen su archivo en disco
        item.job.out_path = unique_path(out.parent, out.name, {i.out_path for i in self.items if not i.finished})
        self.items.append(item)
        self.changed.emit()
        self._pump()

    def cancel(self, item: QueueItem):
        if item.state == QUEUED:
            item.state, item.stage = CANCELLED, t("export.stage.cancelled")
            self.changed.emit()
        elif item.state == AUDIO:
            item.cancel_event.set()  # el hilo de grabación pausa Spotify y termina
        elif item.state == RENDER:
            self._runner.cancel()

    def remove(self, item: QueueItem):
        if item.finished and item in self.items:
            self.items.remove(item)
            self.changed.emit()

    def clear_finished(self):
        self.items = [i for i in self.items if not i.finished]
        self.changed.emit()

    def shutdown(self):
        """Al cerrar la app: cancela lo que esté en curso y lo que espera."""
        for it in list(self.items):
            if not it.finished:
                self.cancel(it)
        stop_runner = getattr(self._runner, "shutdown", None)
        if stop_runner:
            stop_runner()

    @property
    def busy(self) -> bool:
        return self._active is not None

    # ---- motor ----
    def _pump(self):
        if self._active:
            return
        nxt = next((i for i in self.items if i.state == QUEUED), None)
        if not nxt:
            return
        self._active = nxt
        if nxt.audio:
            nxt.state, nxt.stage, nxt.progress = AUDIO, t("export.stage.preparingAudio"), 0.0
            self.changed.emit()
            run_async(lambda: self._make_audio(nxt), lambda wav: self._audio_ready(nxt, wav), lambda e: self._audio_failed(nxt, e))
        else:
            self._render(nxt)

    def _make_audio(self, item: QueueItem) -> Path:
        """En un hilo: audio exacto del tramo -> WAV temporal."""
        a = item.audio
        loop = item.job.loop
        if item.cancel_event.is_set():
            raise RecordingCancelled()
        if a.kind == "spotify":
            loop_options = {"loop_fade_ms": loop.audio_fade_ms} if loop else {}
            samples, rate = record_song_range(self.services.spotify, a.track, a.start_ms, a.end_ms, device=a.device,
                                              offset_ms=a.offset_ms, cancel=item.cancel_event.is_set,
                                              progress=lambda f, text: self._audio_progress.emit(item, f, text), **loop_options)
        elif loop:
            samples, rate = loop_local(a.samples, item.job.start_ms, loop.duration_ms,
                                       a.offset_ms, loop.audio_fade_ms), local.RATE
        else:
            samples, rate = local.segment(a.samples, a.start_ms, a.end_ms, a.offset_ms), local.RATE
        if item.cancel_event.is_set():
            raise RecordingCancelled()
        n = min(round(a.fade_out_ms / 1000 * rate), len(samples)) if not loop else 0
        if n > 0:
            samples[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
        item._audio_duration_ms = len(samples) / max(1, rate) * 1000.0
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="marea_audio_")
        os.close(fd)
        wav = Path(name)
        try:
            local.write_wav(wav, samples, rate)  # sobrescribe el archivo temporal vacío
        except Exception:
            wav.unlink(missing_ok=True)
            raise
        return wav

    def _audio_ready(self, item: QueueItem, wav: Path):
        item.wav = wav
        item.job.audio_path = wav
        dur_ms = getattr(item, "_audio_duration_ms", 0.0)
        if dur_ms > 0.0 and item.job.loop is None:
            item.job.end_ms = item.job.start_ms + dur_ms
        if item.cancel_event.is_set():
            return self._finish(item, CANCELLED, t("export.stage.cancelled"))
        self._render(item)

    def _audio_failed(self, item: QueueItem, error):
        if isinstance(error, RecordingCancelled):
            self._finish(item, CANCELLED, t("export.stage.cancelled"))
        else:
            msg = str(error) if isinstance(error, ProviderError) else t("export.audioFailed")
            self._finish(item, FAILED, t("export.stage.failed"), msg)

    def _on_audio_progress(self, item: QueueItem, fraction: float, text: str):
        if item.state == AUDIO:
            item.stage, item.progress = text, fraction
            self.item_changed.emit(item)

    def _render(self, item: QueueItem):
        item.state, item.stage, item.progress, item.eta = RENDER, t("export.stage.rendering"), 0.0, None
        self.changed.emit()
        self._runner.start(item.job)

    def _on_render_progress(self, done: int, total: int, eta: float):
        if self._active:
            self._active.progress, self._active.eta = done / total, eta
            self.item_changed.emit(self._active)

    def _on_render_done(self, path: str):
        item = self._active
        self._finish(item, DONE, t("export.stage.done"))
        run_async(
            lambda: self._record_history(item),
            lambda thumb: self._thumb_ready(item, thumb),
            lambda e: (log.exception("Failed post-export history step for %s", item.out_path, exc_info=e),
                       self._thumb_ready(item, None)),
        )

    def _record_history(self, item: QueueItem):
        thumb = None
        try:
            thumb = history.make_thumbnail(item.out_path)
        except Exception:
            log.exception("Failed thumbnail generation for %s", item.out_path)
        try:
            history.add({"path": str(item.out_path), "title": item.title, "artist": item.artist,
                         "thumb": str(thumb) if thumb else "", "loop": item.job.loop is not None})
        except Exception:
            log.exception("Failed history entry for %s", item.out_path)
        return thumb

    def _thumb_ready(self, item: QueueItem, thumb):
        item.thumb = thumb
        self.item_done.emit(item)
        self.changed.emit()

    def _on_render_failed(self, message: str, detail: str):
        if detail:
            print(detail)
        self._finish(self._active, FAILED, t("export.stage.failed"),
                     t("export.renderFailed", message=message))

    def _on_render_cancelled(self):
        self._finish(self._active, CANCELLED, t("export.stage.cancelled"))

    def _finish(self, item: QueueItem, state: str, stage: str, error: str = ""):
        item.state, item.stage, item.error, item.eta = state, stage, error, None
        if state == DONE:
            item.progress = 1.0
        if item.wav:
            item.wav.unlink(missing_ok=True)
            item.wav = None
        self._active = None
        self.changed.emit()
        if state == FAILED:
            self.item_failed.emit(item)
        self._pump()
