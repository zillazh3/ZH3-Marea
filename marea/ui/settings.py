"""Settings page body: appearance, Spotify, API keys, export defaults, audio, and experimental labs."""
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from marea.async_util import run_async
from marea.audio.loopback import AudioError
from marea.export.ffmpeg import BACKENDS, CODECS, HW_ORDER, detect_encoders
from marea.model.loop import AUDIO_FADE_MS, AUDIO_FADE_RANGE, VIDEO_FADE_MS, VIDEO_FADE_RANGE
from marea.paths import default_output_dir
from marea.providers.errors import ProviderError
from marea.providers.spotify import REDIRECT_URI
from marea.secrets import KEYS
from marea import i18n
from marea.ui import lang, theme
from marea.ui.widgets import ToggleSwitch, blocked_signals, make_button


CARD_MIN = 640  # narrowest a settings card may get before the grid drops to one column


def _loopback_fn(name: str):
    """Resolve loopback helpers via marea.ui.screens when present for test monkeypatching."""
    screens_mod = sys.modules.get("marea.ui.screens")
    if screens_mod is not None and hasattr(screens_mod, name):
        return getattr(screens_mod, name)
    from marea.audio import loopback
    return getattr(loopback, name)


class SettingsBody(QWidget):
    loop_settings_changed = Signal()

    def __init__(self, settings: QSettings, on_theme, services, notify):
        super().__init__()
        self.services, self.notify, self._waiting = services, notify, False
        self._dev_req = 0
        self._cards, self._cols = [], 0
        self.columns = QHBoxLayout(self)
        self.columns.setAlignment(Qt.AlignTop)
        self.columns.setSpacing(theme.SPACE[4])
        self._col_layouts = [QVBoxLayout(), QVBoxLayout()]
        for col in self._col_layouts:
            col.setSpacing(theme.SPACE[4])
            self.columns.addLayout(col, 1)

        self.settings = settings
        lay = self._card("settings.card.appearance")
        self.combo = QComboBox()
        lang.bind_items(self.combo, [("settings.theme.dark", "dark"), ("settings.theme.light", "light")])
        self.combo.setCurrentIndex(max(0, self.combo.findData(theme.saved_name(settings))))
        self.combo.currentIndexChanged.connect(lambda: on_theme(self.combo.currentData()))
        self._row(lay, "settings.theme.label", self.combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItem(lang.t("settings.language.system"), "system")
        for code, name in i18n.available():  # native names, never translated
            self.lang_combo.addItem(name, code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(settings.value("language", "system"))))
        lang.bind_fn(self.lang_combo, "system-item", lambda: self.lang_combo.setItemText(0, lang.t("settings.language.system")))
        self.lang_combo.currentIndexChanged.connect(self._language_chosen)
        lang_label = QLabel()
        lang_label.setObjectName("Body")
        lang.bind(lang_label, "settings.language.title")
        lang_row = QHBoxLayout()
        lang_row.addWidget(lang_label)
        lang_row.addStretch()
        lang_row.addWidget(self.lang_combo)
        lay.addLayout(lang_row)

        lay = self._card("settings.card.spotify")
        cid_row = QHBoxLayout()
        cid_label = QLabel("Client ID")
        cid_label.setObjectName("Body")
        self.client_id = QLineEdit(services.client_id)
        lang.bind(self.client_id, "settings.spotify.clientIdPlaceholder", "setPlaceholderText")
        self.client_id.setMinimumWidth(340)
        self.client_id.editingFinished.connect(lambda: services.set_client_id(self.client_id.text()))
        cid_row.addWidget(cid_label)
        cid_row.addStretch()
        cid_row.addWidget(self.client_id)
        lay.addLayout(cid_row)

        actions = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.btn_connect = make_button("", "Primary", self._connect)
        lang.bind(self.btn_connect, "settings.spotify.connect")
        self.btn_disconnect = make_button("", "Ghost", services.disconnect_spotify)
        lang.bind(self.btn_disconnect, "settings.spotify.disconnect")
        actions.addWidget(self.status, 1)
        actions.addWidget(self.btn_disconnect)
        actions.addWidget(self.btn_connect)
        lay.addLayout(actions)

        uri_row = QHBoxLayout()
        uri_label = QLabel()
        lang.bind(uri_label, "settings.spotify.redirect")
        uri_label.setObjectName("Caption")
        self.uri = QLineEdit(REDIRECT_URI)
        self.uri.setReadOnly(True)
        copy_btn = make_button("", "Ghost", lambda: QApplication.clipboard().setText(REDIRECT_URI))
        lang.bind(copy_btn, "settings.spotify.copy")
        lay.addWidget(uri_label)
        uri_row.addWidget(self.uri, 1)
        uri_row.addWidget(copy_btn)
        lay.addLayout(uri_row)

        self._keys_section(self._card("settings.card.keys"))
        self._export_section(self._card("settings.card.export"), settings)
        self._audio_section(self._card("settings.card.audio"), settings)
        self._experimental_section(self._card("settings.card.experimental"), settings)
        services.connection_changed.connect(lambda _: self._refresh())
        self._refresh()
        self._relayout()

    def _language_chosen(self):
        pref = self.lang_combo.currentData()
        self.settings.setValue("language", pref)
        lang.apply_preference(pref)

    def _card(self, title_key: str) -> QVBoxLayout:
        """A titled section card; `_relayout` flows the cards into 1 or 2 columns by width."""
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.SPACE[5], theme.SPACE[4], theme.SPACE[5], theme.SPACE[5])
        lay.setSpacing(theme.SPACE[3])
        lay.setAlignment(Qt.AlignTop)
        head = QLabel()
        lang.bind(head, title_key)
        head.setObjectName("H2")
        lay.addWidget(head)
        self._cards.append(card)
        return lay

    def _relayout(self):
        # shortcut: 2 columns only when each card keeps CARD_MIN px (the audio/key rows have wide fields), else 1
        cols = 2 if self.width() >= 2 * CARD_MIN + self.columns.spacing() else 1
        if cols == self._cols:
            return
        self._cols = cols
        for col in self._col_layouts:  # keep the trailing stretch, drop the cards
            while col.count():
                col.takeAt(0)
        for i, c in enumerate(self._cards):  # alternate so both columns end up balanced
            self._col_layouts[i % cols].addWidget(c)
        for col in self._col_layouts:
            col.addStretch()
        self.columns.setStretch(1, 1 if cols == 2 else 0)  # the empty second column must not take half the width

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    @staticmethod
    def _row(lay: QVBoxLayout, key: str | None, *widgets, raw: str = "") -> QHBoxLayout:
        """A label + widgets row. `key` is the label's i18n key; `raw` is a literal label (brand names) used when key is None."""
        row = QHBoxLayout()
        label = QLabel(raw)
        if key is not None:
            lang.bind(label, key)
        label.setObjectName("Body")
        row.addWidget(label)
        row.addStretch()
        for w in widgets:
            row.addWidget(w)
        lay.addLayout(row)
        return row

    def _keys_section(self, lay: QVBoxLayout):
        """Claves de API: viven en el Credential Manager de Windows, no en los ajustes."""
        self.key_edits = {}
        for name, label in KEYS.items():
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.Password)
            edit.setMinimumWidth(260)
            lang.bind(edit, "settings.keys.saved" if self.services.secrets.get(name) else "settings.keys.paste", "setPlaceholderText")
            save = make_button("", "Primary", lambda _=False, n=name, e=edit, lb=label: self._save_key(n, e, lb))
            lang.bind(save, "settings.keys.save")
            clear = make_button("", "Ghost", lambda _=False, n=name, e=edit, lb=label: self._clear_key(n, e, lb))
            lang.bind(clear, "settings.keys.clear")
            self._row(lay, None, edit, save, clear, raw=label)  # label = brand name from marea.secrets.KEYS
            self.key_edits[name] = edit
        note = QLabel()
        lang.bind(note, "settings.keys.note")
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)

    def _save_key(self, name: str, edit: QLineEdit, label: str):
        if not edit.text().strip():
            self.notify(lang.t("settings.keys.pasteFirst", label=label), "error")
            return
        try:
            self.services.secrets.set(name, edit.text())
        except Exception:  # keyring puede fallar si Windows bloquea el Credential Manager
            self.notify(lang.t("settings.keys.storeFailed"), "error")
            return
        edit.clear()
        lang.bind(edit, "settings.keys.saved", "setPlaceholderText")
        self.notify(lang.t("settings.keys.savedToast", label=label), "ok")
        if name == "spicy_lyrics":  # que el usuario sepa ahora, no al exportar, si la llave sirve
            run_async(
                self.services.spicy.check_key,
                lambda _: self.notify(lang.t("settings.keys.spicyAccepted"), "ok"),
                lambda e: self.notify(str(e) if isinstance(e, ProviderError) else lang.t("settings.keys.spicyCheckFailed"), "error"),
            )

    def _clear_key(self, name: str, edit: QLineEdit, label: str):
        self.services.secrets.delete(name)
        edit.clear()
        lang.bind(edit, "settings.keys.paste", "setPlaceholderText")
        self.notify(lang.t("settings.keys.clearedToast", label=label))

    def _export_section(self, lay: QVBoxLayout, settings: QSettings):
        self.out_dir = QLineEdit(str(self.output_dir()))
        self.out_dir.setReadOnly(True)
        self.out_dir.setMinimumWidth(260)
        pick = make_button("", "Ghost", self._choose_folder)
        lang.bind(pick, "settings.export.change")
        self._row(lay, "settings.export.outDir", self.out_dir, pick)
        self.encoder = QComboBox()
        self._enc_found: dict[str, tuple[str, ...]] | None = None  # None while detecting
        for data in ("auto", *BACKENDS):
            self.encoder.addItem("", data)
        lang.bind_fn(self.encoder, "items", self._relabel_encoders)
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(settings.value("export/encoder", "auto"))))
        self.encoder.currentIndexChanged.connect(lambda: settings.setValue("export/encoder", self.encoder.currentData()))
        self._row(lay, "settings.export.encoder", self.encoder)
        self.encoder_note = QLabel()
        lang.bind_fn(self.encoder_note, "setText", self._write_encoder_note)
        self.encoder_note.setObjectName("Caption")
        self.encoder_note.setWordWrap(True)
        lay.addWidget(self.encoder_note)
        run_async(detect_encoders, self._encoders_detected, lambda e: None)

    def _relabel_encoders(self):
        """Item texts from the detection state, so they follow both the language and the detection result."""
        self.encoder.setItemText(self.encoder.findData("auto"), lang.t("settings.export.encoderAuto"))
        for key, label in BACKENDS.items():
            codecs = (self._enc_found or {}).get(key, ())
            if codecs:
                text = f"{label} · {', '.join(CODECS[c] for c in codecs)}"
            elif key == "cpu":
                text = label
            elif self._enc_found is None:
                text = lang.t("settings.export.detecting", label=label)
            else:
                text = lang.t("settings.export.notDetected", label=label)
            self.encoder.setItemText(self.encoder.findData(key), text)

    def _write_encoder_note(self):
        found = self._enc_found
        if found is None:
            self.encoder_note.setText(lang.t("settings.export.searching"))
            return
        gpus = [BACKENDS[k] for k in HW_ORDER if found.get(k)]
        self.encoder_note.setText(lang.t("settings.export.noteGpu", gpus=", ".join(gpus)) if gpus else lang.t("settings.export.noteNoGpu"))

    def _encoders_detected(self, found: dict[str, tuple[str, ...]]):
        """Solo se ofrece lo que esta máquina codifica de verdad (ffmpeg lista encoders que el driver no permite)."""
        self._enc_found = found
        for key in BACKENDS:
            self.encoder.model().item(self.encoder.findData(key)).setEnabled(bool(found.get(key, ())) or key == "cpu")
        self._relabel_encoders()
        if not self.encoder.model().item(self.encoder.currentIndex()).isEnabled():  # GPU que ya no está: no se sobrescribe lo guardado
            with blocked_signals(self.encoder):
                self.encoder.setCurrentIndex(0)
        self._write_encoder_note()

    def output_dir(self) -> Path:
        return Path(self.settings.value("output_dir", str(default_output_dir())))

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, lang.t("settings.export.outDir"), str(self.output_dir()))
        if d:
            self.settings.setValue("output_dir", d)
            self.out_dir.setText(d)

    def _audio_section(self, lay: QVBoxLayout, settings: QSettings):
        self.device = QComboBox()
        self.device.setMinimumWidth(340)
        saved_dev = settings.value("audio/device", "")
        # Populate immediately with placeholder/saved entry so init does not block on WASAPI enumeration.
        self._fill_devices(saved_dev, [])
        self._refresh_devices_async()
        self.device.currentIndexChanged.connect(lambda: settings.setValue("audio/device", self.device.currentData() or ""))
        self.btn_detect = make_button("", "Ghost", self._detect)
        lang.bind(self.btn_detect, "settings.audio.detect")
        lang.bind(self.btn_detect, "settings.audio.detectTip", "setToolTip")
        self._row(lay, "settings.audio.device", self.device, self.btn_detect)
        row2 = QHBoxLayout()
        label2 = QLabel()
        lang.bind(label2, "settings.audio.offset")
        label2.setObjectName("Body")
        self.offset = QSpinBox()
        self.offset.setRange(-500, 500)
        self.offset.setSingleStep(10)
        self.offset.setSuffix(" ms")
        self.offset.setValue(int(float(settings.value("audio/loopback_offset_ms", 0))))
        self.offset.valueChanged.connect(lambda v: settings.setValue("audio/loopback_offset_ms", v))
        self.btn_probe = make_button("", "Ghost", self._probe)
        lang.bind(self.btn_probe, "settings.audio.probe")
        row2.addWidget(label2)
        row2.addStretch()
        row2.addWidget(self.btn_probe)
        row2.addWidget(self.offset)
        lay.addLayout(row2)
        hint = QLabel()
        lang.bind(hint, "settings.audio.hint")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        lay.addWidget(hint)

    def _experimental_section(self, lay: QVBoxLayout, settings: QSettings):
        """Funciones experimentales: apagadas por defecto. Con la función apagada no se importa nada de marea.lab."""
        self.cb_align = ToggleSwitch()
        lang.bind(self.cb_align, "settings.experimental.align")
        self.cb_align.setChecked(settings.value("experimental/align", False, type=bool))
        lay.addWidget(self.cb_align)
        note = QLabel()
        lang.bind(note, "settings.experimental.alignNote")
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)
        self._lab_holder = QVBoxLayout()
        lay.addLayout(self._lab_holder)
        self._lab_panel = None
        self.cb_align.toggled.connect(self._align_toggled)
        if self.cb_align.isChecked():
            self._mount_lab_panel()
        self.cb_wordfx = ToggleSwitch()
        lang.bind(self.cb_wordfx, "settings.experimental.wordfx")
        self.cb_wordfx.setChecked(settings.value("experimental/wordfx", False, type=bool))
        self.cb_wordfx.toggled.connect(lambda on: self.settings.setValue("experimental/wordfx", on))
        lay.addWidget(self.cb_wordfx)
        fx_note = QLabel()
        lang.bind(fx_note, "settings.experimental.wordfxNote")
        fx_note.setObjectName("Caption")
        fx_note.setWordWrap(True)
        lay.addWidget(fx_note)
        self.cb_loop = ToggleSwitch()
        lang.bind(self.cb_loop, "loop.title")
        self.cb_loop.setChecked(settings.value("experimental/loop", False, type=bool))
        lay.addWidget(self.cb_loop)
        loop_note = QLabel()
        lang.bind(loop_note, "settings.experimental.loopNote")
        loop_note.setObjectName("Caption")
        loop_note.setWordWrap(True)
        lay.addWidget(loop_note)
        fades = QWidget()
        fade_layout = QVBoxLayout(fades)
        fade_layout.setContentsMargins(0, 0, 0, 0)
        for key, label, default, bounds in (
            ("export/loop_audio_fade_ms", "loop.audioFade", AUDIO_FADE_MS, AUDIO_FADE_RANGE),
            ("export/loop_video_fade_ms", "loop.videoFade", VIDEO_FADE_MS, VIDEO_FADE_RANGE),
        ):
            spin = QSpinBox()
            spin.setRange(*bounds)
            spin.setValue(settings.value(key, default, type=int))
            spin.setSuffix(" ms")
            lang.bind(spin, label, "setAccessibleName")
            spin.valueChanged.connect(lambda v, k=key: self._save_loop_setting(k, v))
            self._row(fade_layout, label, spin)
        lay.addWidget(fades)
        fades.setVisible(self.cb_loop.isChecked())
        self.cb_loop.toggled.connect(fades.setVisible)
        self.cb_loop.toggled.connect(lambda on: self._save_loop_setting("experimental/loop", on))

    def _save_loop_setting(self, key: str, value):
        self.settings.setValue(key, value)
        if key == "experimental/loop":
            self._remeasure_experimental_card()
        self.loop_settings_changed.emit()

    def _align_toggled(self, on: bool):
        self.settings.setValue("experimental/align", on)
        if on:
            self._mount_lab_panel()
        elif self._lab_panel is not None:
            self._lab_panel.setParent(None)
            self._lab_panel.deleteLater()
            self._lab_panel = None
        self._remeasure_experimental_card()

    def _mount_lab_panel(self):
        if self._lab_panel is None:
            from marea.ui.lab import LabSettingsPanel  # import perezoso: con la función apagada nada del laboratorio se carga
            self._lab_panel = LabSettingsPanel(self.settings, self.notify)
            self._lab_holder.addWidget(self._lab_panel)
            self._lab_panel.show()

    def _remeasure_experimental_card(self):
        card = self._lab_holder.parentWidget()
        for column in self._col_layouts:
            index = column.indexOf(card)
            if index >= 0:
                # Replace the layout item so Qt measures the new height at the same width.
                column.takeAt(index)
                column.insertWidget(index, card)
                break

    def focus_lab_model(self, model_id: str) -> QWidget:
        """Refresh lab status and return the target widget for a given model id."""
        if self._lab_panel is not None:
            self._lab_panel.refresh()
            return self._lab_panel.rows[model_id][1]
        return self.cb_align

    def refresh_lab_status(self):
        """Refresh the lab panel if mounted."""
        if self._lab_panel is not None:
            self._lab_panel.refresh()

    def _fill_devices(self, current: str, devices: list[dict] | None = None):
        """Llena la lista sin tocar el ajuste guardado (clear/addItem disparan currentIndexChanged). Un dispositivo guardado
        que ya no existe se muestra como «no disponible» en vez de caer en silencio a «automático»."""
        self._dev_req += 1
        if devices is None:
            try:
                devices = _loopback_fn("list_capture_devices")()
            except Exception:  # sin PyAudio/WASAPI la lista queda en «automático»
                devices = []
        entries = [("", lambda: lang.t("settings.audio.defaultDevice"))]  # (userData, text) - text re-evaluated on language change
        for d in devices:
            name = d["name"]
            entries.append((name, (lambda n=name: n) if d["loopback"] else (lambda n=name: lang.t("settings.audio.inputDevice", name=n))))
        if current and not any(data == current for data, _ in entries):
            entries.append((current, lambda: lang.t("settings.audio.missing", name=current)))
        with blocked_signals(self.device):
            self.device.clear()
            for data, text in entries:
                self.device.addItem(text(), data)
            self.device.setCurrentIndex(max(0, self.device.findData(current)))
        lang.bind_fn(self.device, "items", lambda: [self.device.setItemText(i, text()) for i, (_, text) in enumerate(entries)])

    def _refresh_devices_async(self):
        self._dev_req += 1
        req = self._dev_req

        def apply_devices(devs: list[dict]):
            if req == self._dev_req:
                self._fill_devices(self.settings.value("audio/device", ""), devs)

        run_async(lambda: _loopback_fn("list_capture_devices")(), apply_devices, lambda _err: None)

    def showEvent(self, e):
        super().showEvent(e)
        with blocked_signals(self.offset):  # el Editor puede haberlo cambiado con «Auto»
            self.offset.setValue(int(float(self.settings.value("audio/loopback_offset_ms", 0))))
        if not self.device.view().isVisible():  # dispositivos que aparecieron desde que arrancó la app (Voicemeeter, USB…)
            self._refresh_devices_async()

    def _detect(self):
        self.btn_detect.setEnabled(False)
        self.notify(lang.t("settings.audio.listening"))

        def done(found: list[dict]):
            self.btn_detect.setEnabled(True)
            live = [d for d in found if d["peak"] > 0.001]
            if not live:
                self.notify(lang.t("settings.audio.noAudioHeard"), "error")
                return
            best = live[0]
            if self.device.findData(best["name"]) < 0:  # apareció después de abrir Ajustes
                self._fill_devices(best["name"], found)
            self.device.setCurrentIndex(self.device.findData(best["name"]))
            self.settings.setValue("audio/device", best["name"])  # explícito: si ya estaba seleccionado no hay señal que lo guarde
            more = lang.t("settings.audio.moreSignal", n=len(live) - 1) if len(live) > 1 else ""
            self.notify(lang.t("settings.audio.detected", name=best["name"], level=f"{min(100, best['peak'] * 100):.0f}", more=more), "ok")

        def failed(e):
            self.btn_detect.setEnabled(True)
            self.notify(str(e) if isinstance(e, AudioError) else lang.t("settings.audio.detectFailed"), "error")

        run_async(lambda: _loopback_fn("scan_levels")(), done, failed)

    def _probe(self):
        self.btn_probe.setEnabled(False)
        self.notify(lang.t("settings.audio.probeListening"))
        name = self.device.currentData() or ""

        def done(peak: float):
            self.btn_probe.setEnabled(True)
            if peak > 0.001:
                self.notify(lang.t("settings.audio.probeOk", level=f"{min(100, peak * 100):.0f}"), "ok")
            else:
                self.notify(lang.t("settings.audio.probeSilent"), "error")

        def failed(e):
            self.btn_probe.setEnabled(True)
            self.notify(str(e) if isinstance(e, AudioError) else lang.t("settings.audio.probeFailed"), "error")

        run_async(lambda: _loopback_fn("probe_level")(name), done, failed)

    def _refresh(self):
        connected = self.services.connected
        self._waiting = self._waiting and not connected
        lang.bind(self.status, "settings.spotify.waiting" if self._waiting
                  else "settings.spotify.connected" if connected else "settings.spotify.disconnected")
        self.btn_connect.setEnabled(not connected and not self._waiting)
        self.btn_disconnect.setEnabled(connected)

    def _connect(self):
        self.services.set_client_id(self.client_id.text())
        if not self.services.client_id:
            self.notify(lang.t("settings.spotify.needClientId"), "error")
            return
        self._waiting = True
        self._refresh()
        self.notify(lang.t("settings.spotify.browserOpened"))
        self.services.connect_spotify(ok=lambda _: self.notify(lang.t("settings.spotify.connectedToast"), "ok"), err=self._failed)

    def _failed(self, error):
        self._waiting = False
        self._refresh()
        self.notify(str(error), "error")
