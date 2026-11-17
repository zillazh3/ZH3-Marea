"""Sidecar worker runner for automatic lyric alignment and model downloads."""
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from marea.i18n import t
from marea.lab import env
from marea.lab.sidecar import SidecarClient, worker_argv

STAGE_NAMES = {  # stage id -> i18n key
    "decode": "lab.stage.decode",
    "separate": "lab.stage.separate",
    "emissions": "lab.stage.emissions",
    "energy": "lab.stage.energy",
    "align": "lab.stage.align",
    "asr": "lab.stage.asr",
    "download": "lab.stage.download",
    "separate_layers": "lab.stage.separateLayers",
}


def stage_text(name: str, state: str, seconds: float = 0.0) -> str:
    label = t(STAGE_NAMES[name]) if name in STAGE_NAMES else name
    if state == "start":
        return t("lab.stage.start", label=label)
    if state == "done":
        return t("lab.stage.done", label=label, seconds=f"{seconds:.1f}")
    if state == "cached":
        return t("lab.stage.cached", label=label)
    if state == "error":
        return t("lab.stage.error", label=label)
    return t("lab.stage.other", label=label, state=state)


class Runner(QObject):
    """Un worker del sidecar con una orden a la vez. Sin red por defecto; `online=True` es el modo descarga."""
    stage = Signal(str)
    done = Signal(str, dict)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, root: Path, online: bool = False, parent=None, client: SidecarClient | None = None):
        super().__init__(parent)
        self.root, self.online = root, online
        self.client = client or SidecarClient(self)
        self._queue: list[tuple[str, dict]] = []
        self._cmd = None
        self._ready = False
        c = self.client
        c.ready.connect(self._on_ready)
        c.stage.connect(lambda _rid, name, state, sec: self.stage.emit(stage_text(name, state, sec)))
        c.result.connect(self._on_result)
        c.failed.connect(self._on_failed)
        c.cancelled.connect(self._on_cancelled)
        c.exited.connect(self._on_exit)
        c.start_failed.connect(self._on_start_failed)

    @property
    def busy(self) -> bool:
        return self._cmd is not None or bool(self._queue)

    def ask(self, cmd: str, **params):
        self._queue.append((cmd, params))
        self._ensure_started()
        self._pump()

    def _ensure_started(self):
        if not self.client.running:
            self._ready = False
            extra = {"HF_HOME": str(self.root / "hf"), "HF_HUB_OFFLINE": "0" if self.online else "1"}
            self.client.start(worker_argv(str(env.env_python(self.root))), extra_env=extra)

    def cancel(self):
        self._queue.clear()
        if self._cmd is not None:
            self.client.cancel()
        else:
            self.client.stop()
            self.cancelled.emit()

    def close(self):
        self._queue.clear()
        self.client.stop()

    def _pump(self):
        if self._ready and self._queue and self._cmd is None:
            cmd, params = self._queue.pop(0)
            self._cmd = cmd
            self.client.request(cmd, cache_dir=str(self.root / "cache"), **params)

    def _on_ready(self):
        self._ready = True
        self._pump()

    def _finish(self) -> str:
        cmd, self._cmd = self._cmd, None
        return cmd or ""

    def _on_result(self, _rid, data):
        cmd = self._finish()
        self.done.emit(cmd, data)
        self._pump()

    def _on_failed(self, _rid, message, _detail):
        cmd = self._finish()
        self.failed.emit(cmd, message)
        self._pump()

    def _on_cancelled(self, _rid):
        self._finish()
        self.cancelled.emit()

    def _on_start_failed(self, message, _detail):
        """El worker no arrancó: las órdenes en cola fallan; el siguiente `ask` vuelve a intentarlo."""
        queued, self._queue, self._cmd, self._ready = self._queue, [], None, False
        for cmd, _params in queued:
            self.failed.emit(cmd, message)

    def _on_exit(self, _code):
        self._ready = False
        if self._queue:
            self._ensure_started()
