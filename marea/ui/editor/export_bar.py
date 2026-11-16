"""Export action bar, options menu, and async NVENC probing for the Editor."""
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu

from marea.async_util import run_async
from marea.export import ffmpeg
from marea.export.job import PRESETS
from marea.ui import theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.widgets import make_button

MARKERS = {"none": "Ninguno", "flash": "Flash de 1 frame", "beep": "Pitido de 120 ms (solo sin audio)"}
MODES = {"lyrics": "Solo letra", "song": "Con canción", "full": "Canción completa"}
OUTROS = {"500": "0.5 s (corte seco)", "2000": "2 s", "4000": "4 s", "8000": "8 s"}


class ExportBarController:
    """Build and synchronize the Editor export bar and options menu."""

    def __init__(self, editor):
        self.editor = editor
        self._nvenc_probing = False

    def build_bar(self) -> QHBoxLayout:
        ed = self.editor
        bar = QHBoxLayout()
        bar.setSpacing(theme.SPACE[3])
        ed.summary = QLabel()
        ed.summary.setObjectName("Caption")
        ed.btn_export = make_button("Exportar", "Primary", ed.export)
        ed.btn_menu = make_button("", "Ghost", tooltip="Modo, formato y opciones de exportación")
        ed.menu = self.build_menu()
        ed.btn_menu.setMenu(ed.menu)
        bar.addStretch()
        bar.addWidget(ed.summary)
        bar.addWidget(ed.btn_export)
        bar.addWidget(ed.btn_menu)
        ed.btn_export.setEnabled(False)
        ed.btn_menu.setEnabled(False)
        self.update_summary()
        return bar

    def build_menu(self) -> QMenu:
        ed = self.editor
        m = QMenu(ed)
        ed.mode_actions = self._radio_submenu(m, None, MODES, "export/mode", "lyrics")
        m.addSeparator()
        ed.preset_actions = self._radio_submenu(m, "Formato", {k: v[0] for k, v in PRESETS.items()}, "export/preset", "tiktok")
        self._radio_submenu(m, "Marcador de inicio", MARKERS, "export/marker", "none")
        for a in self._radio_submenu(m, "Outro", OUTROS, "export/outro_ms", "500").values():
            a.triggered.connect(lambda: ed.project and ed._range_changed())
        ed.encoder_actions = self._radio_submenu(
            m,
            "Codificador",
            {"cpu": "CPU (libx264, mejor compresión)", "nvenc": "NVIDIA NVENC (más rápido)"},
            "export/encoder",
            "cpu",
        )
        m.addSeparator()
        m.addAction("Guardar proyecto…", ed.save_project_dialog)
        m.addAction("Abrir proyecto…", ed.open_project_dialog)
        m.addAction("Reemplazar letra con .ttml…", ed._replace_ttml)
        m.addAction("Carpeta de salida…", ed._choose_folder)
        ed.act_sync_sep = m.addSeparator()
        ed.act_sync = m.addAction("Sincronizar con audio (experimental)…", ed._open_sync)
        ed.act_sync.setVisible(False)
        ed.act_sync_sep.setVisible(False)
        ed.act_fx_sep = m.addSeparator()
        ed.act_fxprofile = m.addAction("Perfil de la canción (efectos por palabra)…", ed._open_fx_profile)
        ed.act_fxprofile.setVisible(False)
        ed.act_fxgallery = m.addAction("Galería de efectos…", ed._open_fx_gallery)
        ed.act_fxgallery.setVisible(False)
        ed.act_fx_sep.setVisible(False)
        m.aboutToShow.connect(self.sync_menu)
        m.aboutToShow.connect(self.probe_nvenc)
        return m

    def sync_menu(self):
        ed = self.editor
        on = ed.settings.value("experimental/align", False, type=bool)
        ed.act_sync.setVisible(on)
        ed.act_sync_sep.setVisible(on)
        fx = ed.settings.value("experimental/wordfx", False, type=bool)
        ed.act_fxprofile.setVisible(fx)
        ed.act_fxgallery.setVisible(fx)
        ed.act_fx_sep.setVisible(fx)
        for key, actions in (("export/preset", ed.preset_actions), ("export/encoder", ed.encoder_actions)):
            current = ed.settings.value(key)
            if current is None and key == "export/encoder":
                current = ffmpeg.default_encoder()
            if current in actions:
                actions[current].setChecked(True)
        self.update_summary()

    def _radio_submenu(self, menu: QMenu, title: str | None, options: dict, key: str, default: str) -> dict:
        ed = self.editor
        target = menu.addMenu(title) if title else menu
        group, actions = QActionGroup(target), {}
        current = ed.settings.value(key, default)
        for value, label in options.items():
            a = target.addAction(label)
            a.setCheckable(True)
            a.setChecked(value == current or (current not in options and value == default))
            a.triggered.connect(
                lambda _=False, k=key, v=value: (
                    ed.settings.setValue(k, v),
                    self.update_summary(),
                    ed._refresh_audio_tab(),
                )
            )
            group.addAction(a)
            actions[value] = a
        return actions

    def probe_nvenc(self):
        if self._nvenc_probing:
            return
        self._nvenc_probing = True

        def check():
            try:
                return ffmpeg.nvenc_available()
            except FileNotFoundError:
                return False

        def apply_nvenc(ok: bool):
            self._nvenc_probing = False
            ed = self.editor
            ed.encoder_actions["nvenc"].setEnabled(ok)
            if not ok and ed.encoder_actions["nvenc"].isChecked():
                ed.encoder_actions["cpu"].setChecked(True)
                ed.settings.setValue("export/encoder", "cpu")
            elif ok and ed.settings.value("export/encoder") is None:
                ed.encoder_actions["nvenc"].setChecked(True)

        def failed(_err):
            self._nvenc_probing = False

        pkg_attr("run_async", run_async)(check, apply_nvenc, failed)

    def update_summary(self):
        ed = self.editor
        preset = PRESETS.get(ed.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])[0]
        ed.summary.setText(f"{MODES[ed.mode]} · {preset}")
