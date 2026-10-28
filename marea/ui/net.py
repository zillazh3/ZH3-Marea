"""Trabajo de red en hilos sin bloquear la UI; los callbacks se ejecutan siempre en el hilo principal."""
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal

_live: set = set()  # referencias hasta que termina cada trabajo


class Job(QObject):
    done = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, ok, err):
        super().__init__()
        self.fn, self.ok, self.err = fn, ok, err
        self.done.connect(self._on_done, Qt.QueuedConnection)  # el emisor es el hilo de trabajo; el receptor, el principal
        self.failed.connect(self._on_failed, Qt.QueuedConnection)

    def _finish(self):
        _live.discard(self)
        self.deleteLater()

    def _on_done(self, result):
        self._finish()
        if self.ok:
            self.ok(result)

    def _on_failed(self, error):
        self._finish()
        if self.err:
            self.err(error)
        else:
            traceback.print_exception(error)


class _Runner(QRunnable):
    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def run(self):
        try:
            result = self.job.fn()
        except Exception as e:  # llega al callback de error en el hilo principal
            self.job.failed.emit(e)
            return
        self.job.done.emit(result)


def run_async(fn, ok=None, err=None) -> Job:
    """Ejecuta fn() en un hilo. Llamar desde el hilo principal. ok(resultado) / err(excepción) vuelven al principal."""
    job = Job(fn, ok, err)
    _live.add(job)
    QThreadPool.globalInstance().start(_Runner(job))
    return job
