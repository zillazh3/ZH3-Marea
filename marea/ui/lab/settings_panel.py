"""Settings panel for installing the lab environment and downloading models."""
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QVBoxLayout, QWidget

from marea.async_util import run_async
from marea.lab import env
from marea.ui import theme
from marea.ui.lab._pkg import pkg_attr
from marea.ui.lab.runner import Runner
from marea.ui.widgets import make_button

LAB_DIR_KEY = "experimental/lab_dir"

COMPONENTS = [
    ("Alineación · español", "hf", "jonatasgrosman/wav2vec2-large-xlsr-53-spanish", "fetch_model", {"lang": "es"}, "1.2 GB"),
    ("Alineación · inglés", "hf", "facebook/wav2vec2-base-960h", "fetch_model", {"lang": "en"}, "0.4 GB"),
    ("Transcripción · Whisper large-v3-turbo", "hf", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "fetch_asr", {}, "1.6 GB"),
    ("Separación de voz · BS-Roformer", "sep", env.SEPARATION_MODEL, "fetch_separator", {}, "0.6 GB"),
    ("Lead/backing vocals · Karaoke", "sep", env.KARAOKE_MODEL, "fetch_separator", {"model": env.KARAOKE_MODEL}, "0.9 GB"),
]


class _Bridge(QObject):
    """Para llevar texto desde un hilo de trabajo al hilo de la UI."""
    text = Signal(str)


class LabSettingsPanel(QWidget):
    """Instalar/desinstalar el entorno pesado y descargar modelos."""

    def __init__(self, settings, notify, parent=None):
        super().__init__(parent)
        self.settings, self.notify = settings, notify
        self.confirm = lambda text: QMessageBox.question(self, "ZH3 Marea", text) == QMessageBox.Yes
        self._cancel = None
        self._runner = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE[2])
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        self.progress = QLabel()
        self.progress.setObjectName("Caption")
        self.progress.setWordWrap(True)
        row = QHBoxLayout()
        self.btn_install = make_button("Instalar entorno (≈ 4.5 GB)", "Primary", self.install)
        self.btn_uninstall = make_button("Desinstalar todo", "Ghost", self.uninstall)
        self.btn_cancel = make_button("Cancelar", "Ghost", self.cancel)
        for b in (self.btn_install, self.btn_uninstall, self.btn_cancel):
            row.addWidget(b)
        row.addStretch()
        lay.addWidget(self.status)
        lay.addLayout(row)
        note = QLabel(
            f"Model folder: {self.root / 'separator-models'}\n"
            "audio-separator code is MIT licensed. Model weight permissions remain unresolved, including the karaoke model. "
            "Review the model terms before clicking Download. Weights are stored separately; downloads start only on request."
        )
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.rows = {}
        for name, kind, ident, cmd, params, size in COMPONENTS:
            r = QHBoxLayout()
            label = QLabel(f"{name} ({size})")
            label.setObjectName("Body")
            state = QLabel()
            state.setObjectName("Caption")
            btn = make_button("Descargar", "Ghost", lambda _=False, c=(kind, ident, cmd, params, name): self.download(*c))
            r.addWidget(label, 1)
            r.addWidget(state)
            r.addWidget(btn)
            lay.addLayout(r)
            self.rows[ident] = (state, btn)
        lay.addWidget(self.progress)
        self.refresh()

    @property
    def root(self) -> Path:
        return env.lab_root(self.settings)

    def refresh(self):
        root, busy = self.root, self._cancel is not None or (self._runner is not None and self._runner.busy)
        installed = env.is_installed(root)
        self.status.setText("Entorno: instalado." if installed else "Entorno: sin instalar. Es una descarga grande (≈ 4.5 GB: Python, torch y librerías).")
        self.btn_install.setEnabled(not installed and not busy)
        self.btn_uninstall.setEnabled(root.exists() and not busy)
        self.btn_cancel.setVisible(busy)
        for _name, kind, ident, _cmd, _params, _size in COMPONENTS:
            state, btn = self.rows[ident]
            ready = env.hf_model_ready(root, ident) if kind == "hf" else env.separator_model_ready(root, ident) and env.separation_installed(root)
            state.setText("Listo" if ready else "Pendiente")
            btn.setEnabled(installed and not ready and not busy)

    def _run_env_step(self, fn, initial_text: str, on_done):
        self._cancel = threading.Event()
        bridge = _Bridge()
        bridge.text.connect(self.progress.setText)
        self._bridge = bridge
        if initial_text:
            self.progress.setText(initial_text)
        pkg_attr("run_async", run_async)(
            lambda: fn(self.root, lambda stage, text: bridge.text.emit(f"{stage}: {text}"), self._cancel),
            on_done,
            self._failed,
        )
        self.refresh()

    def install(self):
        if not self.confirm("Se va a descargar e instalar el entorno de sincronía (≈ 4.5 GB: Python, torch y librerías) dentro de la carpeta «lab» de la app. ¿Continuar?"):
            return
        self._run_env_step(env.install, "Preparando…", lambda _: self._finished("Entorno instalado.", "ok"))

    def uninstall(self):
        if not self.confirm("Se borrará la carpeta «lab» completa: entorno, modelos descargados y cachés de alineación. Tus TTML y audios no se tocan. ¿Continuar?"):
            return
        try:
            env.uninstall(self.root)
        except (OSError, env.InstallError) as e:
            self.notify(f"No pude desinstalar: {e}", "error")
        else:
            self.notify("Sincronía automática desinstalada.", "ok")
        self.refresh()

    def download(self, kind: str, ident: str, cmd: str, params: dict, name: str):
        if kind == "sep" and not env.separation_installed(self.root):
            if not self.confirm("La separación de voz instala además la librería audio-separator y sus dependencias. ¿Continuar?"):
                return
            self._run_env_step(
                env.install_separation,
                "",
                lambda _: (self._finished("Librería de separación instalada.", "ok"), self.download(kind, ident, cmd, params, name)),
            )
            return
        runner_cls = pkg_attr("Runner", Runner)
        self._runner = runner_cls(self.root, online=True, parent=self)
        self._runner.stage.connect(self.progress.setText)
        self._runner.done.connect(lambda _c, _d: (self._finished(f"{name}: listo.", "ok"), self._runner.close()))
        self._runner.failed.connect(lambda _c, msg: (self._failed(RuntimeError(msg)), self._runner.close()))
        self._runner.cancelled.connect(lambda: (self._failed(env.InstallCancelled()), self._runner.close()))
        self.progress.setText(f"Descargando {name}… (puede tardar varios minutos)")
        self._runner.ask(cmd, **params)
        self.refresh()

    def cancel(self):
        if self._cancel is not None:
            self._cancel.set()
        if self._runner is not None:
            self._runner.cancel()
        self.progress.setText("Cancelando…")

    def _finished(self, text: str, kind: str):
        self._cancel = None
        self.progress.setText("")
        self.notify(text, kind)
        self.refresh()

    def _failed(self, e):
        cancelled = isinstance(e, env.InstallCancelled)
        self._cancel = None
        self.progress.setText("")
        self.notify("Cancelado." if cancelled else str(e), "info" if cancelled else "error")
        self.refresh()
