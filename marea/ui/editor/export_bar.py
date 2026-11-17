"""Export action bar and options menu for the Editor."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QMenu, QPushButton

from marea.export import ffmpeg
from marea.export.job import PRESETS
from marea.ui import theme
from marea.ui.widgets import make_button

MARKERS = {"none": "Ninguno", "flash": "Flash de 1 frame", "beep": "Pitido de 120 ms (solo sin audio)"}
MODES = {"lyrics": "Solo letra", "song": "Con canción", "full": "Canción completa"}
OUTROS = {"500": "0.5 s (corte seco)", "2000": "2 s", "4000": "4 s", "8000": "8 s"}


class ExportBarController:
    """Editor export bar: mode chips and format picker always visible, the rest in the options menu.

    Encoder and output folder are machine-level prefs and live only in Settings.
    """

    def __init__(self, editor):
        self.editor = editor

    def build_bar(self) -> QHBoxLayout:
        ed = self.editor
        bar = QHBoxLayout()
        bar.setSpacing(theme.SPACE[3])
        ed.mode_actions = self._radio_actions(None, MODES, "export/mode", "lyrics")
        group = QButtonGroup(ed)
        ed.mode_chips = {}
        for key, action in ed.mode_actions.items():
            chip = QPushButton(action.text())
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(action.isChecked())
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, a=action: a.trigger())
            action.toggled.connect(chip.setChecked)
            group.addButton(chip)
            bar.addWidget(chip)
            ed.mode_chips[key] = chip
        ed.preset_menu = QMenu(ed)
        ed.preset_actions = self._radio_actions(
            ed.preset_menu, {k: v[0] for k, v in PRESETS.items()}, "export/preset", "tiktok")
        ed.btn_format = make_button("", "Ghost", tooltip="Formato del clip")
        ed.btn_format.setMenu(ed.preset_menu)
        ed.summary = QLabel()
        ed.summary.setObjectName("Caption")
        ed.btn_export = make_button("Exportar", "Primary", ed.export)
        ed.btn_menu = make_button("", "Ghost", tooltip="Más opciones de exportación")
        ed.menu = self.build_menu()
        ed.btn_menu.setMenu(ed.menu)
        bar.addWidget(ed.btn_format)
        bar.addStretch()
        bar.addWidget(ed.summary)
        bar.addWidget(ed.btn_export)
        bar.addWidget(ed.btn_menu)
        for w in (ed.btn_export, ed.btn_menu, ed.btn_format, *ed.mode_chips.values()):
            w.setEnabled(False)
        self.update_summary()
        return bar

    def build_menu(self) -> QMenu:
        ed = self.editor
        m = QMenu(ed)
        self._radio_submenu(m, "Marcador de inicio", MARKERS, "export/marker", "none")
        for a in self._radio_submenu(m, "Outro", OUTROS, "export/outro_ms", "500").values():
            a.triggered.connect(lambda: ed.project and ed._range_changed())
        ed.target_actions = self._radio_submenu(
            m, "Archivo", {k: v[1] for k, v in ffmpeg.TARGETS.items()}, "export/target", "mp4")
        for key, a in ed.target_actions.items():
            a.triggered.connect(lambda _=False, k=key: self.target_picked(k))
            if key in ffmpeg.ALPHA_TARGETS:
                a.setToolTip("Exporta sin fondo y con canal alfa: al elegirlo, el fondo cambia a «Transparente».")
        ed.act_alpha_wm = m.addAction("Marca de agua en exports con transparencia")
        ed.act_alpha_wm.setCheckable(True)
        ed.act_alpha_wm.setChecked(ed.settings.value("export/alpha_watermark", True, type=bool))
        ed.act_alpha_wm.toggled.connect(lambda v: ed.settings.setValue("export/alpha_watermark", v))
        m.addSeparator()
        m.addAction("Guardar proyecto…", ed.save_project_dialog)
        m.addAction("Abrir proyecto…", ed.open_project_dialog)
        m.addAction("Reemplazar letra con .ttml…", ed._replace_ttml)
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
        for key, actions in (("export/preset", ed.preset_actions), ("export/target", ed.target_actions)):
            current = ed.settings.value(key)
            if current in actions:
                actions[current].setChecked(True)
        self.update_summary()

    def target_picked(self, target: str):
        """Un formato con alfa necesita el fondo transparente: se cambia solo y se avisa."""
        ed = self.editor
        if target in ffmpeg.ALPHA_TARGETS and ed.project and ed.project.background.source != "transparent":
            ed._bgctl.set_source("transparent")
            ed.notify("Fondo cambiado a «Transparente»: los formatos con canal alfa no llevan fondo.", "info")
        self.update_summary()

    def _radio_submenu(self, menu: QMenu, title: str, options: dict, key: str, default: str) -> dict:
        return self._radio_actions(menu.addMenu(title), options, key, default)

    def _radio_actions(self, target: QMenu | None, options: dict, key: str, default: str) -> dict:
        """Exclusive checkable actions persisted under `key`; added to `target`, or free-standing (for chips) when None."""
        ed = self.editor
        group, actions = QActionGroup(ed), {}
        current = ed.settings.value(key, default)
        for value, label in options.items():
            a = target.addAction(label) if target is not None else QAction(label, ed)
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

    def update_summary(self):
        ed = self.editor
        preset = PRESETS.get(ed.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])[0]
        target = ffmpeg.TARGETS.get(ed.settings.value("export/target", "mp4"), ffmpeg.TARGETS["mp4"])[1]
        ed.btn_format.setText(preset)
        ed.summary.setText(target)  # mode and format are already visible as chips / picker
