"""Background worker runner returning results or exceptions via Qt queued signals."""
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal

_live: set = set()  # hold references until each job finishes


class Job(QObject):
    done = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, ok, err):
        super().__init__()
        self.fn, self.ok, self.err = fn, ok, err
        self.done.connect(self._on_done, Qt.QueuedConnection)
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
        except Exception as e:
            self.job.failed.emit(e)
            return
        self.job.done.emit(result)


def run_async(fn, ok=None, err=None) -> Job:
    """Execute `fn()` on the thread pool; `ok(result)` / `err(exc)` run on the caller's Qt thread."""
    job = Job(fn, ok, err)
    _live.add(job)
    QThreadPool.globalInstance().start(_Runner(job))
    return job
