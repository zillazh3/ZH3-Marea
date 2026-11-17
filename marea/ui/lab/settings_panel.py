"""Settings panel for installing the lab environment and downloading models."""
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QVBoxLayout, QWidget

from marea.async_util import run_async
from marea.lab import env
from marea.ui import lang, theme
from marea.ui.lab._pkg import pkg_attr
from marea.ui.lab.runner import Runner
from marea.ui.widgets import make_button

LAB_DIR_KEY = "experimental/lab_dir"

COMPONENTS = [  # first field: i18n key of the component name
    ("lab.component.alignEs", "hf", "jonatasgrosman/wav2vec2-large-xlsr-53-spanish", "fetch_model", {"lang": "es"}, "1.2 GB"),
    ("lab.component.alignEn", "hf", "facebook/wav2vec2-base-960h", "fetch_model", {"lang": "en"}, "0.4 GB"),
    ("lab.component.asr", "hf", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "fetch_asr", {}, "1.6 GB"),
    ("lab.component.separation", "sep", env.SEPARATION_MODEL, "fetch_separator", {}, "0.6 GB"),
    ("lab.component.karaoke", "sep", env.KARAOKE_MODEL, "fetch_separator", {"model": env.KARAOKE_MODEL}, "0.9 GB"),
]

ENV_STAGES = {  # progress stage id -> i18n key (other stage ids are shown as they come)
    "paquetes": "lab.envStage.packages",
    "verificación": "lab.envStage.verification",
    "separación": "lab.envStage.separation",
}


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
        self.btn_install = make_button("", "Primary", self.install)
        lang.bind(self.btn_install, "lab.settings.install")
        self.btn_uninstall = make_button("", "Ghost", self.uninstall)
        lang.bind(self.btn_uninstall, "lab.settings.uninstall")
        self.btn_cancel = make_button("", "Ghost", self.cancel)
        lang.bind(self.btn_cancel, "lab.common.cancel")
        for b in (self.btn_install, self.btn_uninstall, self.btn_cancel):
            row.addWidget(b)
        row.addStretch()
        lay.addWidget(self.status)
        lay.addLayout(row)
        note = QLabel()
        lang.bind(note, "lab.settings.modelNote", folder=self.root / "separator-models")
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.rows = {}
        for name, kind, ident, cmd, params, size in COMPONENTS:
            r = QHBoxLayout()
            label = QLabel()
            lang.bind_fn(label, "setText", lambda label=label, name=name, size=size: label.setText(lang.t("lab.settings.componentLabel", name=lang.t(name), size=size)))
            label.setObjectName("Body")
            state = QLabel()
            state.setObjectName("Caption")
            btn = make_button("", "Ghost", lambda _=False, c=(kind, ident, cmd, params, name): self.download(*c))
            lang.bind(btn, "lab.settings.download")
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
        lang.bind(self.status, "lab.settings.envInstalled" if installed else "lab.settings.envMissing")
        self.btn_install.setEnabled(not installed and not busy)
        self.btn_uninstall.setEnabled(root.exists() and not busy)
        self.btn_cancel.setVisible(busy)
        for _name, kind, ident, _cmd, _params, _size in COMPONENTS:
            state, btn = self.rows[ident]
            ready = env.hf_model_ready(root, ident) if kind == "hf" else env.separator_model_ready(root, ident) and env.separation_installed(root)
            lang.bind(state, "lab.settings.ready" if ready else "lab.settings.pending")
            btn.setEnabled(installed and not ready and not busy)

    def _run_env_step(self, fn, initial_text: str, on_done):
        self._cancel = threading.Event()
        bridge = _Bridge()
        bridge.text.connect(self.progress.setText)
        self._bridge = bridge
        if initial_text:
            self.progress.setText(initial_text)
        pkg_attr("run_async", run_async)(
            lambda: fn(self.root, lambda stage, text: bridge.text.emit(f"{lang.t(ENV_STAGES[stage]) if stage in ENV_STAGES else stage}: {text}"), self._cancel),
            on_done,
            self._failed,
        )
        self.refresh()

    def install(self):
        if not self.confirm(lang.t("lab.settings.installConfirm")):
            return
        self._run_env_step(env.install, lang.t("lab.settings.preparing"), lambda _: self._finished(lang.t("lab.settings.envInstalledOk"), "ok"))

    def uninstall(self):
        if not self.confirm(lang.t("lab.settings.uninstallConfirm")):
            return
        try:
            env.uninstall(self.root)
        except (OSError, env.InstallError) as e:
            self.notify(lang.t("lab.settings.uninstallFailed", error=e), "error")
        else:
            self.notify(lang.t("lab.settings.uninstalled"), "ok")
        self.refresh()

    def download(self, kind: str, ident: str, cmd: str, params: dict, name: str):
        if kind == "sep" and not env.separation_installed(self.root):
            if not self.confirm(lang.t("lab.settings.separationConfirm")):
                return
            self._run_env_step(
                env.install_separation,
                "",
                lambda _: (self._finished(lang.t("lab.settings.separationInstalled"), "ok"), self.download(kind, ident, cmd, params, name)),
            )
            return
        runner_cls = pkg_attr("Runner", Runner)
        self._runner = runner_cls(self.root, online=True, parent=self)
        self._runner.stage.connect(self.progress.setText)
        self._runner.done.connect(lambda _c, _d: (self._finished(lang.t("lab.settings.componentDone", name=lang.t(name)), "ok"), self._runner.close()))
        self._runner.failed.connect(lambda _c, msg: (self._failed(RuntimeError(msg)), self._runner.close()))
        self._runner.cancelled.connect(lambda: (self._failed(env.InstallCancelled()), self._runner.close()))
        self.progress.setText(lang.t("lab.settings.downloading", name=lang.t(name)))
        self._runner.ask(cmd, **params)
        self.refresh()

    def cancel(self):
        if self._cancel is not None:
            self._cancel.set()
        if self._runner is not None:
            self._runner.cancel()
        self.progress.setText(lang.t("lab.common.cancelling"))

    def _finished(self, text: str, kind: str):
        self._cancel = None
        self.progress.setText("")
        self.notify(text, kind)
        self.refresh()

    def _failed(self, e):
        cancelled = isinstance(e, env.InstallCancelled)
        self._cancel = None
        self.progress.setText("")
        self.notify(lang.t("lab.common.cancelled") if cancelled else str(e), "info" if cancelled else "error")
        self.refresh()
