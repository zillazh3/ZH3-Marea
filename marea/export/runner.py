"""Lado Qt del export: lanza el render en un proceso aparte y emite señales de progreso."""
import multiprocessing as mp
import queue as queue_mod

from PySide6.QtCore import QObject, QTimer, Signal

from marea.export.job import ExportJob, worker_main


class ExportRunner(QObject):
    progress = Signal(int, int, float)  # hechos, total, segundos restantes
    finished = Signal(str)  # ruta del mp4
    failed = Signal(str, str)  # mensaje para el usuario, detalle técnico
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = self._queue = self._cancel = None
        self._timer = QTimer(self, interval=50)
        self._timer.timeout.connect(self._poll)

    @property
    def running(self) -> bool:
        return self._proc is not None

    def start(self, job: ExportJob):
        """Proceso aparte: la UI no se traba, no hay GIL compartido y el contexto GL es propio."""
        ctx = mp.get_context("spawn")
        self._queue, self._cancel = ctx.Queue(), ctx.Event()
        self._job = job
        self._proc = ctx.Process(target=worker_main, args=(job, self._queue, self._cancel), daemon=True)
        self._proc.start()
        self._timer.start()

    def cancel(self):
        if self._cancel:
            self._cancel.set()  # el worker mata ffmpeg y borra el mp4 a medias

    def _poll(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.progress.emit(*msg[1:])
                    continue
                self._cleanup()
                {"done": lambda: self.finished.emit(msg[1]), "cancelled": self.cancelled.emit,
                 "error": lambda: self.failed.emit(msg[1], msg[2])}[kind]()
                return
        except queue_mod.Empty:
            pass
        if self._proc and not self._proc.is_alive() and self._queue.empty():  # murió sin avisar
            self._cleanup()
            self._job.out_path.unlink(missing_ok=True)
            self.failed.emit("El proceso de render se cerró inesperadamente.", "")

    def _cleanup(self):
        self._timer.stop()
        if self._proc:
            self._proc.join(timeout=5)
            if self._proc.is_alive():
                self._proc.terminate()
        self._proc = self._queue = self._cancel = None
