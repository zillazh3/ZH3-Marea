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
from marea.ui import lang, theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.waveform import WaveformWidget
from marea.ui.widgets import blocked_signals, make_button

LOOPBACK_OFFSET_RANGE, LOCAL_OFFSET_RANGE = (-500, 500), (-15000, 15000)  # ms
AUTO_RECORD_MS = 25_000
AUDIO_FILTER = "audio.fileFilter"  # i18n key


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
        lang.bind_items(ed.cmb_source, [("audio.source.spotify", "spotify"), ("audio.source.local", "local")])
        ed.cmb_source.currentIndexChanged.connect(self.source_changed)
        if ed.services:
            ed.services.now_playing.connect(self.on_playback)
        lay.addWidget(ed.cmb_source)

        ed.lb_audio = QLabel()
        ed.lb_audio.setObjectName("Caption")
        ed.lb_audio.setWordWrap(True)
        lay.addWidget(ed.lb_audio)

        ed.btn_audio_file = make_button("", "Ghost", self.choose_audio)
        lang.bind(ed.btn_audio_file, "audio.chooseFile")
        lay.addWidget(ed.btn_audio_file)

        ed.wave = WaveformWidget()
        ed.wave.seek_requested.connect(ed._on_wave_seek)
        lay.addWidget(ed.wave)

        row = QHBoxLayout()
        ed.sl_offset = QSlider(Qt.Horizontal)
        ed.lb_offset = QLabel("0 ms")
        ed.lb_offset.setObjectName("Caption")
        ed.lb_offset.setMinimumWidth(52)
        ed.sl_offset.valueChanged.connect(self.offset_changed)
        offset_label = QLabel()
        lang.bind(offset_label, "audio.offset")
        offset_label.setObjectName("Muted")
        ed.btn_auto = make_button("", "Ghost", self.auto_offset)
        lang.bind(ed.btn_auto, "audio.auto")
        lang.bind(ed.btn_auto, "audio.autoTip", "setToolTip")
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
        trk = ed.project.track
        if trk and trk.provider == "spotify" and trk.id_for("spotify"):
            return trk
        return ed._linked

    def spotify_ready(self) -> bool:
        return self.spotify_track() is not None

    def spotify_hint(self) -> str:
        ed = self.editor
        if not (ed.services and ed.services.connected):
            return lang.t("audio.hint.connect")
        return lang.t("audio.hint.nothingPlaying")

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
            fname = ed.project.audio_file.name if ed.project.audio_file else None
            lang.bind_fn(ed.lb_audio, "setText", lambda: ed.lb_audio.setText(
                lang.t("audio.localHint", name=fname or lang.t("audio.noFile"))))
        else:
            device = ed.settings.value("audio/device", "")
            trk = self.spotify_track()
            if trk:
                linked = bool(ed._linked and not ed.project.track)

                def spotify_text():
                    link = lang.t("audio.linkedWith", artist=trk.artist, title=trk.title) if linked else ""
                    return lang.t("audio.spotifyHint", link=link, device=device or lang.t("audio.defaultOutput"))

                lang.bind_fn(ed.lb_audio, "setText", lambda: ed.lb_audio.setText(spotify_text()))
            else:
                lang.bind_fn(ed.lb_audio, "setText", lambda: ed.lb_audio.setText(self.spotify_hint()))
        self.refresh_wave()

    def offset_changed(self, v: int):
        ed = self.editor
        if not ed.project:
            return
        ed.lb_offset.setText(f"{v:+d} ms")
        if ed.cmb_source.currentData() == "local":
            ed.project.audio_offset_ms = float(v)
            ed.preview.set_audio_offset(float(v))
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
                ed.notify(lang.t("audio.pickFirst"), "error")
                return
            samples, audio_file = p.audio_samples, p.audio_file

            def job():
                from marea.wordfx import audiofeat

                stem = audiofeat.find_cached_stem(audio_file) if audio_file else None
                return align.detect_offset(samples, local.RATE, lyrics, 0.0, hi, vocal_stem=stem)

            ed.notify(lang.t("audio.measuring"))
        else:
            if not ed._spotify_ready():
                ed.notify(self.spotify_hint(), "error")
                return
            if ed.queue.busy:
                ed.notify(lang.t("audio.exportBusy"), "error")
                return
            start, end = clip_range(lyrics, ed.first, ed.last)
            end = min(end, start + AUTO_RECORD_MS)
            client, track, device = ed.services.spotify, self.spotify_track(), ed.settings.value("audio/device", "")
            record_fn = pkg_attr("record_song_range", record_song_range)

            def job():
                segment, rate = record_fn(client, track, start, end, device=device, offset_ms=0.0, fade_ms=0.0)
                return align.detect_offset(segment, rate, lyrics, start, hi)

            ed.notify(lang.t("audio.willPlay", seconds=f"{(end - start) / 1000:.0f}"))
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
            ed.notify(lang.t("audio.discarded"), "info")
            return
        if not d.reliable:
            ed.notify(lang.t("audio.unreliable"), "error")
            return
        v = max(ed.sl_offset.minimum(), min(ed.sl_offset.maximum(), round(d.offset_ms)))
        ed.sl_offset.setValue(v)
        ed.notify(lang.t("audio.detected", offset=f"{v:+d}"), "ok")

    def _auto_failed(self, e):
        ed = self.editor
        self._auto_busy = False
        ed.btn_auto.setEnabled(True)
        known = (ValueError, AudioError, ProviderError)
        ed.notify(str(e) if isinstance(e, known) else lang.t("audio.measureFailed"), "error")

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
        path, _ = QFileDialog.getOpenFileName(ed, lang.t("audio.chooseTitle"), "", lang.t(AUDIO_FILTER))
        if not path:
            return
        ed.notify(lang.t("audio.reading"))
        project = ed.project

        def done(samples):
            project.audio_file, project.audio_samples, project.audio_offset_ms = Path(path), samples, 0.0
            if ed.project is project:
                ed.preview.set_audio_samples(samples, 0.0)
                ed._sync_auto_opening_trim()
                self.refresh()
                ed._refresh_cinematic_tab()
                ed._wordfx_sync()

        pkg_attr("run_async", run_async)(
            lambda: local.decode(path),
            done,
            lambda e: ed.notify(str(e) if isinstance(e, AudioError) else lang.t("audio.readFailed"), "error"),
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
            ed.notify(lang.t("audio.pickForExport"), "error")
            return None
        return AudioSpec("local", start_ms, end_ms, samples=p.audio_samples, offset_ms=p.audio_offset_ms)
