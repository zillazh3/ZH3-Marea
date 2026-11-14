"""Settings page body: appearance, Spotify, API keys, export defaults, audio, and experimental labs."""
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from marea.async_util import run_async
from marea.audio.loopback import AudioError
from marea.export.ffmpeg import nvenc_available
from marea.export.job import PRESETS
from marea.paths import default_output_dir
from marea.providers.errors import ProviderError
from marea.providers.spotify import REDIRECT_URI
from marea.secrets import KEYS
from marea.ui import theme
from marea.ui.widgets import blocked_signals, make_button


def _loopback_fn(name: str):
    """Resolve loopback helpers via marea.ui.screens when present for test monkeypatching."""
    screens_mod = sys.modules.get("marea.ui.screens")
    if screens_mod is not None and hasattr(screens_mod, name):
        return getattr(screens_mod, name)
    from marea.audio import loopback
    return getattr(loopback, name)


class SettingsBody(QWidget):
    def __init__(self, settings: QSettings, on_theme, services, notify):
        super().__init__()
        self.services, self.notify, self._waiting = services, notify, False
        self._dev_req = 0
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignTop)
        lay.setSpacing(theme.SPACE[4])

        self.settings = settings
        self._heading(lay, "Apariencia")
        self.combo = QComboBox()
        self.combo.addItem("Oscuro", "dark")
        self.combo.addItem("Claro", "light")
        self.combo.setCurrentIndex(max(0, self.combo.findData(theme.saved_name(settings))))
        self.combo.currentIndexChanged.connect(lambda: on_theme(self.combo.currentData()))
        self._row(lay, "Tema", self.combo)

        self._heading(lay, "Spotify")
        cid_row = QHBoxLayout()
        cid_label = QLabel("Client ID")
        cid_label.setObjectName("Body")
        self.client_id = QLineEdit(services.client_id)
        self.client_id.setPlaceholderText("Client ID de tu app en developer.spotify.com")
        self.client_id.setMinimumWidth(340)
        self.client_id.editingFinished.connect(lambda: services.set_client_id(self.client_id.text()))
        cid_row.addWidget(cid_label)
        cid_row.addStretch()
        cid_row.addWidget(self.client_id)
        lay.addLayout(cid_row)

        actions = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.btn_connect = make_button("Conectar", "Primary", self._connect)
        self.btn_disconnect = make_button("Desconectar", "Ghost", services.disconnect_spotify)
        actions.addWidget(self.status, 1)
        actions.addWidget(self.btn_disconnect)
        actions.addWidget(self.btn_connect)
        lay.addLayout(actions)

        uri_row = QHBoxLayout()
        uri_label = QLabel("Redirect URI (regístralo en el dashboard de tu app)")
        uri_label.setObjectName("Caption")
        self.uri = QLineEdit(REDIRECT_URI)
        self.uri.setReadOnly(True)
        copy_btn = make_button("Copiar", "Ghost", lambda: QApplication.clipboard().setText(REDIRECT_URI))
        lay.addWidget(uri_label)
        uri_row.addWidget(self.uri, 1)
        uri_row.addWidget(copy_btn)
        lay.addLayout(uri_row)

        self._keys_section(lay)
        self._export_section(lay, settings)
        self._audio_section(lay, settings)
        self._experimental_section(lay, settings)
        services.connection_changed.connect(lambda _: self._refresh())
        self._refresh()

    @staticmethod
    def _heading(lay: QVBoxLayout, text: str):
        title = QLabel(text)
        title.setObjectName("H2")
        lay.addSpacing(theme.SPACE[2])
        lay.addWidget(title)

    @staticmethod
    def _row(lay: QVBoxLayout, text: str, *widgets) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(text)
        label.setObjectName("Body")
        row.addWidget(label)
        row.addStretch()
        for w in widgets:
            row.addWidget(w)
        lay.addLayout(row)
        return row

    def _keys_section(self, lay: QVBoxLayout):
        """Claves de API: viven en el Credential Manager de Windows, no en los ajustes."""
        self._heading(lay, "Claves de API")
        self.key_edits = {}
        for name, label in KEYS.items():
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.Password)
            edit.setMinimumWidth(260)
            edit.setPlaceholderText("Guardada" if self.services.secrets.get(name) else "Pega tu clave")
            save = make_button("Guardar", "Primary", lambda _=False, n=name, e=edit, lb=label: self._save_key(n, e, lb))
            clear = make_button("Borrar", "Ghost", lambda _=False, n=name, e=edit, lb=label: self._clear_key(n, e, lb))
            self._row(lay, label, edit, save, clear)
            self.key_edits[name] = edit
        note = QLabel(
            "Las claves se guardan en el Credential Manager de Windows (cifradas para tu usuario), nunca en la carpeta "
            "de la app. Spicy Lyrics: llave de cliente (sl_pk_…) de developers.spicylyrics.org con «Allow requests with no "
            "Origin header» activado; da letra por sílaba de Apple Music y de la comunidad. LRCLIB no necesita clave. "
            "Tidal no está disponible: no tiene acceso oficial con cuenta."
        )
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)

    def _save_key(self, name: str, edit: QLineEdit, label: str):
        if not edit.text().strip():
            self.notify(f"Pega primero la clave de {label}.", "error")
            return
        try:
            self.services.secrets.set(name, edit.text())
        except Exception:  # keyring puede fallar si Windows bloquea el Credential Manager
            self.notify("Windows no me dejó guardar la clave. Revisa que el Credential Manager esté disponible.", "error")
            return
        edit.clear()
        edit.setPlaceholderText("Guardada")
        self.notify(f"Clave de {label} guardada.", "ok")
        if name == "spicy_lyrics":  # que el usuario sepa ahora, no al exportar, si la llave sirve
            run_async(
                self.services.spicy.check_key,
                lambda _: self.notify("Spicy Lyrics aceptó la llave.", "ok"),
                lambda e: self.notify(str(e) if isinstance(e, ProviderError) else "No pude comprobar la llave de Spicy Lyrics.", "error"),
            )

    def _clear_key(self, name: str, edit: QLineEdit, label: str):
        self.services.secrets.delete(name)
        edit.clear()
        edit.setPlaceholderText("Pega tu clave")
        self.notify(f"Clave de {label} borrada.")

    def _export_section(self, lay: QVBoxLayout, settings: QSettings):
        self._heading(lay, "Exportación")
        self.out_dir = QLineEdit(str(self.output_dir()))
        self.out_dir.setReadOnly(True)
        self.out_dir.setMinimumWidth(260)
        pick = make_button("Cambiar…", "Ghost", self._choose_folder)
        self._row(lay, "Carpeta de salida", self.out_dir, pick)
        self.preset = QComboBox()
        for key, (label, *_) in PRESETS.items():
            self.preset.addItem(label, key)
        self.preset.setCurrentIndex(max(0, self.preset.findData(settings.value("export/preset", "tiktok"))))
        self.preset.currentIndexChanged.connect(lambda: settings.setValue("export/preset", self.preset.currentData()))
        self._row(lay, "Formato", self.preset)
        self.encoder = QComboBox()
        self.encoder.addItem("CPU (libx264, mejor compresión)", "cpu")
        self.encoder.addItem("NVIDIA NVENC (más rápido)", "nvenc")
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(settings.value("export/encoder", "cpu"))))
        self.encoder.currentIndexChanged.connect(lambda: settings.setValue("export/encoder", self.encoder.currentData()))
        self._row(lay, "Codificador", self.encoder)
        run_async(nvenc_available, self._nvenc_checked, lambda e: None)  # sin NVENC no se ofrece

    def _nvenc_checked(self, ok: bool):
        item = self.encoder.model().item(self.encoder.findData("nvenc"))
        item.setEnabled(ok)
        if not ok and self.encoder.currentData() == "nvenc":
            self.encoder.setCurrentIndex(0)
        elif ok and self.settings.value("export/encoder") is None:
            idx = self.encoder.findData("nvenc")
            if idx >= 0:
                self.encoder.setCurrentIndex(idx)

    def output_dir(self) -> Path:
        return Path(self.settings.value("output_dir", str(default_output_dir())))

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta de salida", str(self.output_dir()))
        if d:
            self.settings.setValue("output_dir", d)
            self.out_dir.setText(d)

    def _audio_section(self, lay: QVBoxLayout, settings: QSettings):
        self._heading(lay, "Audio")
        self.device = QComboBox()
        self.device.setMinimumWidth(340)
        saved_dev = settings.value("audio/device", "")
        # Populate immediately with placeholder/saved entry so init does not block on WASAPI enumeration.
        self._fill_devices(saved_dev, [])
        self._refresh_devices_async()
        self.device.currentIndexChanged.connect(lambda: settings.setValue("audio/device", self.device.currentData() or ""))
        self.btn_detect = make_button(
            "Detectar",
            "Ghost",
            self._detect,
            "Escucha todos los dispositivos con algo sonando en Spotify y elige el que tiene señal",
        )
        self._row(lay, "Dispositivo de captura", self.device, self.btn_detect)
        row2 = QHBoxLayout()
        label2 = QLabel("Desfase de audio (loopback)")
        label2.setObjectName("Body")
        self.offset = QSpinBox()
        self.offset.setRange(-500, 500)
        self.offset.setSingleStep(10)
        self.offset.setSuffix(" ms")
        self.offset.setValue(int(float(settings.value("audio/loopback_offset_ms", 0))))
        self.offset.valueChanged.connect(lambda v: settings.setValue("audio/loopback_offset_ms", v))
        self.btn_probe = make_button("Probar captura", "Ghost", self._probe)
        row2.addWidget(label2)
        row2.addStretch()
        row2.addWidget(self.btn_probe)
        row2.addWidget(self.offset)
        lay.addLayout(row2)
        hint = QLabel(
            "Reproduce algo en Spotify y pulsa «Detectar»: Marea escucha todos los dispositivos y elige por el que sale su audio. "
            "«Probar captura» comprueba el que tengas elegido. Si usas Voicemeeter, su salida por defecto suele dar silencio: "
            "elige una de sus entradas «Voicemeeter Out B1/B2» o un altavoz físico."
        )
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        lay.addWidget(hint)

    def _experimental_section(self, lay: QVBoxLayout, settings: QSettings):
        """Funciones experimentales: apagadas por defecto. Con la función apagada no se importa nada de marea.lab."""
        self._heading(lay, "Funciones experimentales")
        self.cb_align = QCheckBox("Sincronización automática de letras con el audio")
        self.cb_align.setChecked(settings.value("experimental/align", False, type=bool))
        lay.addWidget(self.cb_align)
        note = QLabel(
            "Alinea una letra con el audio (palabra, sílaba y vocal aproximada) y te deja revisarla antes de exportar un TTML NUEVO: tu "
            "archivo original nunca se modifica. Usa la GPU y, al instalarla, descarga varios GB. Aparece como «Sincronizar con audio» en el "
            "menú del Editor."
        )
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)
        self._lab_holder = QVBoxLayout()
        lay.addLayout(self._lab_holder)
        self._lab_panel = None
        self.cb_align.toggled.connect(self._align_toggled)
        if self.cb_align.isChecked():
            self._mount_lab_panel()
        self.cb_wordfx = QCheckBox("Efectos semánticos por palabra")
        self.cb_wordfx.setChecked(settings.value("experimental/wordfx", False, type=bool))
        self.cb_wordfx.toggled.connect(lambda on: self.settings.setValue("experimental/wordfx", on))
        lay.addWidget(self.cb_wordfx)
        fx_note = QLabel(
            "Palabras sueltas de la letra reaccionan a su significado y emoción (tiempo, pérdida, amor…), solo dentro de la palabra. "
            "Se ve en el preview y en el export. La biblioteca de categorías es editable: docs/wordfx.md."
        )
        fx_note.setObjectName("Caption")
        fx_note.setWordWrap(True)
        lay.addWidget(fx_note)

    def _align_toggled(self, on: bool):
        self.settings.setValue("experimental/align", on)
        if on:
            self._mount_lab_panel()
        elif self._lab_panel is not None:
            self._lab_panel.setParent(None)
            self._lab_panel.deleteLater()
            self._lab_panel = None

    def _mount_lab_panel(self):
        if self._lab_panel is None:
            from marea.ui.lab import LabSettingsPanel  # import perezoso: con la función apagada nada del laboratorio se carga
            self._lab_panel = LabSettingsPanel(self.settings, self.notify)
            self._lab_holder.addWidget(self._lab_panel)

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
        with blocked_signals(self.device):
            self.device.clear()
            self.device.addItem("Salida por defecto de Windows (automático)", "")
            for d in devices:
                self.device.addItem(d["name"] + ("" if d["loopback"] else " · entrada"), d["name"])
            if current and self.device.findData(current) < 0:
                self.device.addItem(f"{current} · no disponible", current)
            self.device.setCurrentIndex(max(0, self.device.findData(current)))

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
        self.notify("Escuchando todos los dispositivos 1.5 segundos… deja algo sonando en Spotify.")

        def done(found: list[dict]):
            self.btn_detect.setEnabled(True)
            live = [d for d in found if d["peak"] > 0.001]
            if not live:
                self.notify("No oí audio en ningún dispositivo. Reproduce algo en Spotify (con el volumen arriba) y vuelve a intentar.", "error")
                return
            best = live[0]
            if self.device.findData(best["name"]) < 0:  # apareció después de abrir Ajustes
                self._fill_devices(best["name"], found)
            self.device.setCurrentIndex(self.device.findData(best["name"]))
            self.settings.setValue("audio/device", best["name"])  # explícito: si ya estaba seleccionado no hay señal que lo guarde
            more = f" También hay señal en {len(live) - 1} más; revisa la lista si no es el correcto." if len(live) > 1 else ""
            self.notify(f"Spotify suena por «{best['name']}» (nivel {min(100, best['peak'] * 100):.0f} %). Lo dejé seleccionado.{more}", "ok")

        def failed(e):
            self.btn_detect.setEnabled(True)
            self.notify(str(e) if isinstance(e, AudioError) else "No pude escuchar los dispositivos.", "error")

        run_async(lambda: _loopback_fn("scan_levels")(), done, failed)

    def _probe(self):
        self.btn_probe.setEnabled(False)
        self.notify("Escuchando 1.5 segundos…")
        name = self.device.currentData() or ""

        def done(peak: float):
            self.btn_probe.setEnabled(True)
            if peak > 0.001:
                self.notify(f"Se captó audio (nivel {min(100, peak * 100):.0f} %). Esa es la fuente correcta.", "ok")
            else:
                self.notify(
                    "No se captó audio. Reproduce algo en Spotify y vuelve a probar; si usas Voicemeeter, elige «Voicemeeter Out B1» "
                    "o un altavoz físico en vez de su salida virtual.",
                    "error",
                )

        def failed(e):
            self.btn_probe.setEnabled(True)
            self.notify(str(e) if isinstance(e, AudioError) else "No pude probar la captura.", "error")

        run_async(lambda: _loopback_fn("probe_level")(name), done, failed)

    def _refresh(self):
        connected = self.services.connected
        self._waiting = self._waiting and not connected
        self.status.setText(
            "Esperando la autorización en tu navegador…" if self._waiting
            else "Conectado" if connected else "Sin conectar"
        )
        self.btn_connect.setEnabled(not connected and not self._waiting)
        self.btn_disconnect.setEnabled(connected)

    def _connect(self):
        self.services.set_client_id(self.client_id.text())
        if not self.services.client_id:
            self.notify("Pega primero el Client ID de tu app de Spotify.", "error")
            return
        self._waiting = True
        self._refresh()
        self.notify("Se abrió tu navegador para autorizar a ZH3 Marea en Spotify.")
        self.services.connect_spotify(ok=lambda _: self.notify("Spotify conectado.", "ok"), err=self._failed)

    def _failed(self, error):
        self._waiting = False
        self._refresh()
        self.notify(str(error), "error")
