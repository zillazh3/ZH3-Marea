"""Cola de exportaciones: de una en una; cada trabajo puede grabar/preparar su audio y luego renderizar el video."""
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from marea.audio import local
from marea.audio.session import RecordingCancelled, record_song_range
from marea.export import history
from marea.export.job import ExportJob
from marea.export.runner import ExportRunner
from marea.providers.errors import ProviderError
from marea.ui.net import run_async

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
    stage: str = "En cola"
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
        self.items.append(item)
        self.changed.emit()
        self._pump()

    def cancel(self, item: QueueItem):
        if item.state == QUEUED:
            item.state, item.stage = CANCELLED, "Cancelado"
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
            nxt.state, nxt.stage, nxt.progress = AUDIO, "Preparando el audio…", 0.0
            self.changed.emit()
            run_async(lambda: self._make_audio(nxt), lambda wav: self._audio_ready(nxt, wav), lambda e: self._audio_failed(nxt, e))
        else:
            self._render(nxt)

    def _make_audio(self, item: QueueItem) -> Path:
        """En un hilo: audio exacto del tramo -> WAV temporal."""
        a = item.audio
        if a.kind == "spotify":
            samples, rate = record_song_range(self.services.spotify, a.track, a.start_ms, a.end_ms, device=a.device,
                                              offset_ms=a.offset_ms, cancel=item.cancel_event.is_set,
                                              progress=lambda f, text: self._audio_progress.emit(item, f, text))
        else:
            samples, rate = local.segment(a.samples, a.start_ms, a.end_ms, a.offset_ms), local.RATE
        n = min(round(a.fade_out_ms / 1000 * rate), len(samples))
        if n > 0:
            samples[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="marea_audio_")
        os.close(fd)
        local.write_wav(name, samples, rate)  # sobrescribe el archivo temporal vacío
        return Path(name)

    def _audio_ready(self, item: QueueItem, wav: Path):
        item.wav = wav
        item.job.audio_path = wav
        if item.cancel_event.is_set():
            return self._finish(item, CANCELLED, "Cancelado")
        self._render(item)

    def _audio_failed(self, item: QueueItem, error):
        if isinstance(error, RecordingCancelled):
            self._finish(item, CANCELLED, "Cancelado")
        else:
            msg = str(error) if isinstance(error, ProviderError) else "No pude preparar el audio. Intenta de nuevo."
            self._finish(item, FAILED, "Falló", msg)

    def _on_audio_progress(self, item: QueueItem, fraction: float, text: str):
        if item.state == AUDIO:
            item.stage, item.progress = text, fraction
            self.item_changed.emit(item)

    def _render(self, item: QueueItem):
        item.state, item.stage, item.progress, item.eta = RENDER, "Renderizando video…", 0.0, None
        self.changed.emit()
        self._runner.start(item.job)

    def _on_render_progress(self, done: int, total: int, eta: float):
        if self._active:
            self._active.progress, self._active.eta = done / total, eta
            self.item_changed.emit(self._active)

    def _on_render_done(self, path: str):
        item = self._active
        self._finish(item, DONE, "Listo")
        run_async(lambda: self._record_history(item), lambda thumb: self._thumb_ready(item, thumb), lambda e: None)

    def _record_history(self, item: QueueItem):
        thumb = history.make_thumbnail(item.out_path)
        history.add({"path": str(item.out_path), "title": item.title, "artist": item.artist,
                     "thumb": str(thumb) if thumb else ""})
        return thumb

    def _thumb_ready(self, item: QueueItem, thumb):
        item.thumb = thumb
        self.item_done.emit(item)
        self.changed.emit()

    def _on_render_failed(self, message: str, detail: str):
        if detail:
            print(detail)
        self._finish(self._active, FAILED, "Falló",
                     f"{message}. Revisa que ffmpeg funcione y que haya espacio en la carpeta de salida.")

    def _on_render_cancelled(self):
        self._finish(self._active, CANCELLED, "Cancelado")

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
