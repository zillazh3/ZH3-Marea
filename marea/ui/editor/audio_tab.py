"""Audio tab controller for the Editor."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from marea.async_util import run_async
from marea.audio import align, local
from marea.audio.loopback import AudioError
from marea.audio.session import record_song_range
from marea.export.job import clip_range
from marea.export.queue import AudioSpec
from marea.providers.errors import ProviderError
from marea.providers.spotify import Track
from marea.ui import theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.waveform import WaveformWidget
from marea.ui.widgets import blocked_signals, make_button

LOOPBACK_OFFSET_RANGE, LOCAL_OFFSET_RANGE = (-500, 500), (-15000, 15000)  # ms
AUTO_RECORD_MS = 25_000
AUDIO_FILTER = "Audio (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus);;Todos los archivos (*.*)"


class AudioController:
    """Manage Editor audio source selection, offset detection, waveform, and local file decode."""

    def __init__(self, editor):
        self.editor = editor
        self._auto_busy = False

    def build_tab(self) -> QWidget:
        ed = self.editor
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, theme.SPACE[3], 0, 0)
        lay.setSpacing(theme.SPACE[2])

        ed.cmb_source = QComboBox()
        ed.cmb_source.addItem("Loopback de Spotify", "spotify")
        ed.cmb_source.addItem("Archivo de audio", "local")
        ed.cmb_source.currentIndexChanged.connect(self.source_changed)
        if ed.services:
            ed.services.now_playing.connect(self.on_playback)
        lay.addWidget(ed.cmb_source)

        ed.lb_audio = QLabel()
        ed.lb_audio.setObjectName("Caption")
        ed.lb_audio.setWordWrap(True)
        lay.addWidget(ed.lb_audio)

        ed.btn_audio_file = make_button("Elegir archivo…", "Ghost", self.choose_audio)
        lay.addWidget(ed.btn_audio_file)

        ed.wave = WaveformWidget()
        lay.addWidget(ed.wave)

        row = QHBoxLayout()
        ed.sl_offset = QSlider(Qt.Horizontal)
        ed.lb_offset = QLabel("0 ms")
        ed.lb_offset.setObjectName("Caption")
        ed.lb_offset.setMinimumWidth(52)
        ed.sl_offset.valueChanged.connect(self.offset_changed)
        offset_label = QLabel("Desfase")
        offset_label.setObjectName("Muted")
        ed.btn_auto = make_button(
            "Auto",
            "Ghost",
            self.auto_offset,
            "Detecta el desfase comparando los ataques del audio con los inicios de sílaba de la letra",
        )
        row.addWidget(offset_label)
        row.addWidget(ed.sl_offset, 1)
        row.addWidget(ed.lb_offset)
        row.addWidget(ed.btn_auto)
        lay.addLayout(row)
        lay.addStretch()
        return w

    def spotify_track(self) -> Track | None:
        ed = self.editor
        if not (ed.project and ed.services and ed.services.connected):
            return None
        return ed.project.track or ed._linked

    def spotify_ready(self) -> bool:
        return self.spotify_track() is not None

    def spotify_hint(self) -> str:
        ed = self.editor
        if not (ed.services and ed.services.connected):
            return "Conecta tu cuenta de Spotify en Ajustes; o elige «Archivo de audio»."
        return "No hay nada sonando en Spotify. Dale play a la canción y se vinculará sola; o elige «Archivo de audio»."

    def link_playing(self):
        ed = self.editor
        s = ed.services
        if ed.project and not ed.project.track and not ed._linked and s and s.connected and s.playback:
            ed._linked = s.playback.track

    def source_changed(self, *_):
        ed = self.editor
        if ed.project and ed.cmb_source.currentData() == "spotify":
            self.link_playing()
            if not ed._spotify_ready() and ed.services and ed.services.connected:
                ed.services.poll()
        ed._sync_auto_opening_trim()
        self.refresh()
        ed._refresh_cinematic_tab()

    def on_playback(self, _pb):
        ed = self.editor
        if ed.project and ed.cmb_source.currentData() == "spotify":
            self.link_playing()
            self.refresh()

    def pick_default_source(self):
        ed = self.editor
        with blocked_signals(ed.cmb_source):
            ed.cmb_source.setCurrentIndex(0 if ed._spotify_ready() else 1)
        self.refresh()

    def refresh(self, *_):
        ed = self.editor
        if not ed.project:
            return
        source = ed.cmb_source.currentData()
        is_local = source == "local"
        ed.btn_audio_file.setVisible(is_local)
        ed.wave.setVisible(is_local)
        lo, hi = LOCAL_OFFSET_RANGE if is_local else LOOPBACK_OFFSET_RANGE
        value = ed.project.audio_offset_ms if is_local else float(ed.settings.value("audio/loopback_offset_ms", 0))
        with blocked_signals(ed.sl_offset):
            ed.sl_offset.setRange(lo, hi)
            ed.sl_offset.setValue(round(value))
        ed.lb_offset.setText(f"{round(value):+d} ms")
        if is_local:
            name = ed.project.audio_file.name if ed.project.audio_file else "ningún archivo"
            ed.lb_audio.setText(
                f"Archivo: {name}. Mueve el desfase hasta que los golpes de la forma de onda coincidan "
                "con las líneas azules (inicio de cada verso)."
            )
        else:
            device = ed.settings.value("audio/device", "") or "la salida por defecto de Windows"
            t = self.spotify_track()
            if t:
                link = f"Vinculada con «{t.artist} – {t.title}» (la que suena en tu Spotify). " if ed._linked and not ed.project.track else ""
                ed.lb_audio.setText(
                    f"{link}Se reproducirá la canción en tu Spotify y se grabará lo que suene por {device}. "
                    "Requiere Spotify Premium y que nada más suene en ese momento. El desfase corrige la latencia de tu equipo."
                )
            else:
                ed.lb_audio.setText(self.spotify_hint())
        self.refresh_wave()

    def offset_changed(self, v: int):
        ed = self.editor
        if not ed.project:
            return
        ed.lb_offset.setText(f"{v:+d} ms")
        if ed.cmb_source.currentData() == "local":
            ed.project.audio_offset_ms = float(v)
            ed._sync_auto_opening_trim()
            self.refresh_wave()
            ed._refresh_cinematic_tab()
            ed._wordfx_sync()
        else:
            ed.settings.setValue("audio/loopback_offset_ms", v)

    def auto_offset(self):
        ed = self.editor
        p = ed.project
        if not p or self._auto_busy:
            return
        is_local = ed.cmb_source.currentData() == "local"
        hi = (LOCAL_OFFSET_RANGE if is_local else LOOPBACK_OFFSET_RANGE)[1]
        lyrics = p.lyrics
        if is_local:
            if p.audio_samples is None:
                ed.notify("Elige primero un archivo de audio para medir el desfase.", "error")
                return
            samples, audio_file = p.audio_samples, p.audio_file

            def job():
                from marea.wordfx import audiofeat

                stem = audiofeat.find_cached_stem(audio_file) if audio_file else None
                return align.detect_offset(samples, local.RATE, lyrics, 0.0, hi, vocal_stem=stem)

            ed.notify("Midiendo el desfase…")
        else:
            if not ed._spotify_ready():
                ed.notify(self.spotify_hint(), "error")
                return
            if ed.queue.busy:
                ed.notify("Espera a que termine la exportación en curso: Spotify no puede reproducir dos cosas a la vez.", "error")
                return
            start, end = clip_range(lyrics, ed.first, ed.last)
            end = min(end, start + AUTO_RECORD_MS)
            client, track, device = ed.services.spotify, self.spotify_track(), ed.settings.value("audio/device", "")
            record_fn = pkg_attr("record_song_range", record_song_range)

            def job():
                segment, rate = record_fn(client, track, start, end, device=device, offset_ms=0.0, fade_ms=0.0)
                return align.detect_offset(segment, rate, lyrics, start, hi)

            ed.notify(f"Se reproducirá la canción en Spotify unos {(end - start) / 1000:.0f} s para medir el desfase…")
        self._auto_busy = True
        ed.btn_auto.setEnabled(False)
        origin = self._auto_input()
        pkg_attr("run_async", run_async)(job, lambda d: self._auto_done(d, origin), self._auto_failed)

    def _auto_input(self) -> tuple:
        ed = self.editor
        p, source = ed.project, ed.cmb_source.currentData()
        if source == "local":
            return p, source, p.audio_samples if p else None
        t = self.spotify_track()
        return p, source, (t.id if t else None, ed.settings.value("audio/device", ""))

    def _auto_done(self, d: "align.Detection", origin: tuple):
        ed = self.editor
        self._auto_busy = False
        ed.btn_auto.setEnabled(True)
        now = self._auto_input()
        same_audio = now[2] is origin[2] if origin[1] == "local" else now[2] == origin[2]
        if now[0] is not origin[0] or now[1] != origin[1] or not same_audio:
            ed.notify("Descarté la medición del desfase: cambiaste de proyecto o de audio mientras se medía.", "info")
            return
        if not d.reliable:
            ed.notify(
                "No pude medir el desfase con seguridad: la letra y el audio no coinciden lo bastante en este tramo. "
                "Prueba con un tramo con más voz o ajústalo a mano.",
                "error",
            )
            return
        v = max(ed.sl_offset.minimum(), min(ed.sl_offset.maximum(), round(d.offset_ms)))
        ed.sl_offset.setValue(v)
        ed.notify(f"Desfase detectado: {v:+d} ms.", "ok")

    def _auto_failed(self, e):
        ed = self.editor
        self._auto_busy = False
        ed.btn_auto.setEnabled(True)
        known = (ValueError, AudioError, ProviderError)
        ed.notify(str(e) if isinstance(e, known) else "No pude medir el desfase. Intenta de nuevo.", "error")

    def refresh_wave(self):
        ed = self.editor
        p = ed.project
        if not p or p.audio_samples is None or ed.cmb_source.currentData() != "local":
            ed.wave.set_data(None, (0, 1), 0, [], (0, 1))
            return
        start, end = ed._clip_range()
        view = (max(0.0, start - 1500), end + 1500)
        markers = [l.start for l in p.lyrics.lines if view[0] <= l.start <= view[1]]
        ed.wave.set_data(p.audio_samples, view, p.audio_offset_ms, markers, (start, end))

    def choose_audio(self):
        ed = self.editor
        path, _ = QFileDialog.getOpenFileName(ed, "Elegir audio", "", AUDIO_FILTER)
        if not path:
            return
        ed.notify("Leyendo el audio…")
        project = ed.project

        def done(samples):
            project.audio_file, project.audio_samples, project.audio_offset_ms = Path(path), samples, 0.0
            if ed.project is project:
                ed._sync_auto_opening_trim()
                self.refresh()
                ed._refresh_cinematic_tab()
                ed._wordfx_sync()

        pkg_attr("run_async", run_async)(
            lambda: local.decode(path),
            done,
            lambda e: ed.notify(str(e) if isinstance(e, AudioError) else "No pude leer ese archivo de audio.", "error"),
        )

    def audio_spec(self, start_ms: float, end_ms: float) -> AudioSpec | None:
        ed = self.editor
        p = ed.project
        if ed.cmb_source.currentData() == "spotify":
            if not ed._spotify_ready():
                ed.notify(self.spotify_hint(), "error")
                return None
            return AudioSpec(
                "spotify",
                start_ms,
                end_ms,
                track=self.spotify_track(),
                device=ed.settings.value("audio/device", ""),
                offset_ms=float(ed.settings.value("audio/loopback_offset_ms", 0)),
            )
        if p.audio_samples is None:
            ed.notify("Elige un archivo de audio en la pestaña Audio para exportar con canción.", "error")
            return None
        return AudioSpec("local", start_ms, end_ms, samples=p.audio_samples, offset_ms=p.audio_offset_ms)
