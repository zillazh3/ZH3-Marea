"""Cliente Qt del proceso aparte (sidecar/worker.py): lo lanza, le manda peticiones y traduce sus mensajes a señales.

Mismo patrón que export/runner.py (proceso aparte + señales), pero por stdio. La UI nunca se bloquea y el proceso no comparte nada
con la app: si hay que matarlo se mata con todo su árbol."""
import json
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from marea.export.ffmpeg import NO_WINDOW

GRACE_MS = 3000  # tiempo que se le da al worker para cancelar por las buenas antes de matarlo
STDERR_TAIL = 4000


def sidecar_root() -> Path:
    """Carpeta que contiene el paquete `sidecar/`: la raíz del repo, o los datos empaquetados en el portable."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def worker_argv(python: str) -> list[str]:
    return [python, "-u", "-X", "utf8", "-m", "sidecar.worker"]


def kill_tree(pid: int):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        os.kill(pid, 9)


class SidecarClient(QObject):
    ready = Signal()
    stage = Signal(str, str, str, float)  # id de la petición, etapa, estado (start|done|cached|error), segundos
    stage_detail = Signal(str, str, dict)  # id, etapa, detalle medido (dispositivo, VRAM pico…) cuando la etapa lo trae
    result = Signal(str, dict)
    failed = Signal(str, str, str)  # id, mensaje para el usuario, detalle técnico
    cancelled = Signal(str)
    exited = Signal(int)

    def __init__(self, parent=None, log_path: Path | None = None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buf, self._n, self._pending = b"", 0, set()
        self._log = log_path
        self.stderr_tail = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def start(self, argv: list[str], cwd: str | Path | None = None, extra_env: dict | None = None):
        if self.running:
            return
        self._buf, self.stderr_tail = b"", ""
        self._proc = p = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONUTF8", "1")
        for k, v in (extra_env or {}).items():  # p. ej. HF_HOME: los modelos se descargan dentro de lab/
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setWorkingDirectory(str(cwd or sidecar_root()))
        p.readyReadStandardOutput.connect(self._on_stdout)
        p.readyReadStandardError.connect(self._on_stderr)
        p.finished.connect(self._on_finished)
        p.errorOccurred.connect(self._on_error)
        p.start(argv[0], argv[1:])

    def request(self, cmd: str, **params) -> str:
        """Manda una petición y devuelve su id. El resultado llega por `result`/`failed`/`cancelled`."""
        if not self.running:
            raise RuntimeError("El proceso de sincronía no está en marcha.")
        self._n += 1
        rid = f"r{self._n}"
        self._pending.add(rid)
        self._send({"cmd": cmd, "id": rid, **params})
        return rid

    def cancel(self, grace_ms: int = GRACE_MS):
        """Pide cancelar lo pendiente; si el worker no responde en `grace_ms` (p. ej. está dentro de una llamada larga a un modelo)
        se mata el árbol de procesos. Hay que volver a llamar a `start` para la siguiente petición."""
        ids = set(self._pending)
        for rid in ids:
            self._send({"cmd": "cancel", "id": rid})
        if ids:
            QTimer.singleShot(grace_ms, lambda: self._force(ids))

    def stop(self):
        if self.running:
            self._pending.clear()
            kill_tree(self._proc.processId())
            self._proc.waitForFinished(2000)

    # ---- internos ----
    def _send(self, msg: dict):
        if self.running:
            self._proc.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))

    def _force(self, ids: set):
        left = ids & self._pending
        if not left or not self.running:
            return
        kill_tree(self._proc.processId())
        self._pending -= left
        for rid in sorted(left):
            self.cancelled.emit(rid)

    def _on_stdout(self):
        self._buf += bytes(self._proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            try:
                ev = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue  # el protocolo es solo JSON; cualquier otra línea es ruido de una librería
            self._dispatch(ev)

    def _dispatch(self, ev: dict):
        kind, rid = ev.get("event"), ev.get("id", "")
        if kind == "ready":
            self.ready.emit()
        elif kind == "stage":
            self.stage.emit(rid, ev["name"], ev["state"], float(ev.get("seconds", 0.0)))
            if ev.get("detail"):
                self.stage_detail.emit(rid, ev["name"], ev["detail"])
        elif kind == "result" and rid in self._pending:
            self._pending.discard(rid)
            self.result.emit(rid, ev.get("data", {}))
        elif kind == "error" and rid in self._pending:
            self._pending.discard(rid)
            self.failed.emit(rid, ev.get("message", ""), ev.get("detail", ""))
        elif kind == "cancelled" and rid in self._pending:
            self._pending.discard(rid)
            self.cancelled.emit(rid)

    def _on_stderr(self):
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        self.stderr_tail = (self.stderr_tail + data)[-STDERR_TAIL:]
        if self._log:
            with open(self._log, "a", encoding="utf-8") as f:
                f.write(data)

    def _on_error(self, error):
        if error == QProcess.FailedToStart:
            self._fail_pending("No pude iniciar el proceso de sincronía.")

    def _on_finished(self, code, _status):
        self._on_stdout()  # lo último que dijo antes de salir (un resultado, p. ej.) aún puede estar en el pipe
        self._on_stderr()
        self._fail_pending("El proceso de sincronía se cerró inesperadamente.")
        self.exited.emit(code)

    def _fail_pending(self, message: str):
        pending, self._pending = sorted(self._pending), set()
        for rid in pending:
            self.failed.emit(rid, message, self.stderr_tail)
