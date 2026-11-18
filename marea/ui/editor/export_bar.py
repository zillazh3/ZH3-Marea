"""Export action bar and options menu for the Editor."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QMenu, QPushButton

from marea.export import ffmpeg
from marea.export.job import PRESETS
from marea.ui import lang, theme
from marea.ui.widgets import make_button

# option value -> i18n key (resolved by lang.bind where the menu/chip is built)
MARKERS = {"none": "exportBar.marker.none", "flash": "exportBar.marker.flash", "beep": "exportBar.marker.beep"}
MODES = {"lyrics": "exportBar.mode.lyrics", "song": "exportBar.mode.song", "full": "exportBar.mode.full"}
OUTROS = {"500": "exportBar.outro.500", "2000": "exportBar.outro.2000", "4000": "exportBar.outro.4000", "8000": "exportBar.outro.8000"}
PRESET_KEYS = {"tiktok": "exportBar.preset.tiktok", "square": "exportBar.preset.square", "wide": "exportBar.preset.wide"}
TARGET_KEYS = {"mp4": "exportBar.target.mp4", "webm": "exportBar.target.webm", "prores": "exportBar.target.prores",
               "png": "exportBar.target.png"}


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
            chip = QPushButton()
            lang.bind(chip, MODES[key])
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
        ed.preset_actions = self._radio_actions(ed.preset_menu, {k: PRESET_KEYS[k] for k in PRESETS}, "export/preset", "tiktok")
        ed.btn_format = make_button("", "Ghost")
        lang.bind(ed.btn_format, "exportBar.formatTip", "setToolTip")
        ed.btn_format.setMenu(ed.preset_menu)
        ed.summary = QLabel()
        ed.summary.setObjectName("Caption")
        ed.btn_export = make_button("", "Primary", ed.export)
        lang.bind(ed.btn_export, "exportBar.export")
        ed.btn_menu = make_button("", "Ghost")
        lang.bind(ed.btn_menu, "exportBar.moreTip", "setToolTip")
        ed.menu = self.build_menu()
        ed.btn_menu.setMenu(ed.menu)
        bar.addWidget(ed.btn_format)
        bar.addStretch()
        bar.addWidget(ed.summary)
        bar.addWidget(ed.btn_export)
        bar.addWidget(ed.btn_menu)
        for w in (ed.btn_export, ed.btn_menu, ed.btn_format, *ed.mode_chips.values()):
            w.setEnabled(False)
        lang.bind_fn(ed.summary, "summary", self.update_summary)  # also relabels btn_format from the (bound) action texts
        return bar

    def build_menu(self) -> QMenu:
        ed = self.editor
        m = QMenu(ed)
        self._radio_submenu(m, "exportBar.markerTitle", MARKERS, "export/marker", "none")
        for a in self._radio_submenu(m, "exportBar.outroTitle", OUTROS, "export/outro_ms", "500").values():
            a.triggered.connect(lambda: ed.project and ed._range_changed())
        ed.target_actions = self._radio_submenu(
            m, "exportBar.targetTitle", {k: TARGET_KEYS[k] for k in ffmpeg.TARGETS}, "export/target", "mp4")
        for key, a in ed.target_actions.items():
            a.triggered.connect(lambda _=False, k=key: self.target_picked(k))
            if key in ffmpeg.ALPHA_TARGETS:
                lang.bind(a, "exportBar.alphaTip", "setToolTip")
        ed.codec_menu = m.addMenu("")
        lang.bind(ed.codec_menu, "exportBar.codecTitle", "setTitle")
        ed.codec_actions = self._radio_actions(ed.codec_menu, ffmpeg.CODECS, "export/codec", "h264", raw=True)  # codec names are not translated
        lang.bind(ed.codec_actions["h264"], "exportBar.codec.h264Tip", "setToolTip")
        lang.bind(ed.codec_actions["hevc"], "exportBar.codec.hevcTip", "setToolTip")
        lang.bind(ed.codec_actions["av1"], "exportBar.codec.av1Tip", "setToolTip")
        ed.act_alpha_wm = m.addAction("")
        lang.bind(ed.act_alpha_wm, "exportBar.alphaWatermark")
        ed.act_alpha_wm.setCheckable(True)
        ed.act_alpha_wm.setChecked(ed.settings.value("export/alpha_watermark", True, type=bool))
        ed.act_alpha_wm.toggled.connect(lambda v: (ed.settings.setValue("export/alpha_watermark", v), ed.refresh_loop()))
        m.addSeparator()
        lang.bind(m.addAction("", ed.save_project_dialog), "exportBar.saveProject")
        lang.bind(m.addAction("", ed.open_project_dialog), "exportBar.openProject")
        lang.bind(m.addAction("", ed._replace_ttml), "exportBar.replaceLyrics")
        ed.act_sync_sep = m.addSeparator()
        ed.act_sync = m.addAction("", ed._open_sync)
        lang.bind(ed.act_sync, "exportBar.syncAudio")
        ed.act_sync.setVisible(False)
        ed.act_sync_sep.setVisible(False)
        ed.act_fx_sep = m.addSeparator()
        ed.act_fxprofile = m.addAction("", ed._open_fx_profile)
        lang.bind(ed.act_fxprofile, "exportBar.fxProfile")
        ed.act_fxprofile.setVisible(False)
        ed.act_fxgallery = m.addAction("", ed._open_fx_gallery)
        lang.bind(ed.act_fxgallery, "exportBar.fxGallery")
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
        for key, actions in (("export/preset", ed.preset_actions), ("export/target", ed.target_actions),
                             ("export/codec", ed.codec_actions)):
            current = ed.settings.value(key)
            if current in actions:
                actions[current].setChecked(True)
        found = ffmpeg.detected_encoders()  # None mientras se detecta: no se bloquea el menú
        for codec, a in ed.codec_actions.items():
            a.setEnabled(found is None or any(codec in cs for cs in found.values()))
        ed.codec_menu.setEnabled(ed.settings.value("export/target", "mp4") not in ffmpeg.ALPHA_TARGETS)  # WebM/ProRes/PNG traen su códec
        self.update_summary()

    def target_picked(self, target: str):
        """Un formato con alfa necesita el fondo transparente: se cambia solo y se avisa."""
        ed = self.editor
        if target in ffmpeg.ALPHA_TARGETS and ed.project and ed.project.background.source != "transparent":
            ed._bgctl.set_source("transparent")
            ed.notify(lang.t("exportBar.transparentSwitched"), "info")
        ed._update_summary()

    def _radio_submenu(self, menu: QMenu, title_key: str, options: dict, key: str, default: str) -> dict:
        sub = menu.addMenu("")
        lang.bind(sub, title_key, "setTitle")
        return self._radio_actions(sub, options, key, default)

    def _radio_actions(self, target: QMenu | None, options: dict, key: str, default: str, raw: bool = False) -> dict:
        """Exclusive checkable actions persisted under `key`; added to `target`, or free-standing (for chips) when None.
        `options` maps value -> i18n key (or value -> literal label when `raw`)."""
        ed = self.editor
        group, actions = QActionGroup(ed), {}
        current = ed.settings.value(key, default)
        for value, label in options.items():
            a = target.addAction("") if target is not None else QAction(ed)
            if raw:
                a.setText(label)
            else:
                lang.bind(a, label)
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
        preset_id = ed.settings.value("export/preset", "tiktok")
        target_id = ed.settings.value("export/target", "mp4")
        # the labels live in the (language-bound) menu actions
        ed.btn_format.setText(ed.preset_actions[preset_id if preset_id in PRESETS else "tiktok"].text())
        target = ed.target_actions[target_id if target_id in ffmpeg.TARGETS else "mp4"].text()
        # mode and format are already visible as chips / picker; the codec only exists for the MP4
        codec = ffmpeg.CODECS.get(ed.settings.value("export/codec", "h264"), ffmpeg.CODECS["h264"])
        ed.summary.setText(f"{target} · {codec}" if ed.settings.value("export/target", "mp4") == "mp4" else target)
