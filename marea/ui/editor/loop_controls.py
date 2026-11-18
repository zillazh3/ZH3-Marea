"""Per-project loop controls in the Editor."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from marea.async_util import run_async
from marea.audio import local
from marea.audio.analysis import analyze_audio, cached_audio_analysis
from marea.export.job import clip_range
from marea.model.loop import (
    AUDIO_FADE_MS, AUDIO_FADE_RANGE, BAR_NUDGE_MS, VIDEO_FADE_MS, VIDEO_FADE_RANGE, LoopSpec, suggest_bar_end,
)
from marea.ui import lang, theme
from marea.ui.widgets import ToggleSwitch, blocked_signals, fmt_ms, make_button


class LoopControls:
    def __init__(self, editor):
        self.ed = editor
        self.requested = False
        self.adjusted_end = None
        self.proposal = None
        self.analyzing = False
        self._analysis_token = 0

    def build(self):
        self.row = QWidget()
        rows = QVBoxLayout(self.row)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(theme.SPACE[1])
        main = QHBoxLayout()
        self.switch = ToggleSwitch()
        lang.bind(self.switch, "loop.title")
        self.switch.toggled.connect(self.toggle)
        main.addWidget(self.switch)
        self.duration = QLabel()
        self.duration.setObjectName("Body")
        main.addWidget(self.duration)
        self.copy = make_button("", "Ghost", self.copy_duration)
        lang.bind(self.copy, "loop.copy")
        main.addWidget(self.copy)
        main.addStretch()
        self.adjust = make_button("", "Ghost", self.suggest)
        lang.bind(self.adjust, "loop.adjust")
        main.addWidget(self.adjust)
        rows.addLayout(main)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("Caption")
        rows.addWidget(self.note)
        proposal_row = QHBoxLayout()
        self.proposal_text = QLabel()
        self.proposal_text.setWordWrap(True)
        proposal_row.addWidget(self.proposal_text, 1)
        self.accept = make_button("", "Primary", self.accept_proposal)
        lang.bind(self.accept, "loop.accept")
        proposal_row.addWidget(self.accept)
        self.dismiss = make_button("", "Ghost", self.invalidate_proposal)
        lang.bind(self.dismiss, "loop.dismiss")
        proposal_row.addWidget(self.dismiss)
        rows.addLayout(proposal_row)
        self.row.setAttribute(Qt.WA_AlwaysShowToolTips)
        self.row.hide()
        return self.row

    def build_preview_switch(self):
        self.inspect = ToggleSwitch()
        lang.bind(self.inspect, "loop.inspect")
        self.inspect.toggled.connect(lambda on: self.ed.preview.inspect_seam(on))
        self.inspect.hide()
        return self.inspect

    def toggle(self, on: bool):
        self.requested = on
        self.invalidate_proposal(refresh=False)
        self.ed.refresh_loop()

    def invalidate_proposal(self, *, refresh=True):
        self._analysis_token += 1
        self.analyzing = False
        self.proposal = None
        if refresh:
            self.ed.refresh_loop()

    def selection_changed(self, old: tuple[int, int]):
        if old != (self.ed.first, self.ed.last):
            self.adjusted_end = None
            self.invalidate_proposal(refresh=False)

    def restore(self, state: dict):
        self.requested = state.get("loop") is True
        end = state.get("loop_end_ms")
        self.adjusted_end = None
        with blocked_signals(self.inspect):
            self.inspect.setChecked(False)
        if type(end) in (int, float) and self.ed.project.lyrics.lines:
            _, minimum = clip_range(self.ed.project.lyrics, self.ed.first, self.ed.last, 0)
            if minimum <= end <= minimum + BAR_NUDGE_MS + 1000 / 60:
                self.adjusted_end = end
        self.invalidate_proposal(refresh=False)

    def unavailable(self, full: bool) -> str:
        if full:
            return "loop.fullUnavailable"
        if self.ed.mode != "lyrics" and self.ed.settings.value("export/target", "mp4") == "png":
            return "loop.pngUnavailable"
        return ""

    def spec(self, start: float, end: float, first: int, last: int, *, full=False) -> LoopSpec | None:
        ed = self.ed
        if not self.requested or not ed.settings.value("experimental/loop", False, type=bool) or self.unavailable(full):
            return None
        audio = max(AUDIO_FADE_RANGE[0], min(AUDIO_FADE_RANGE[1], ed.settings.value("export/loop_audio_fade_ms", AUDIO_FADE_MS, type=int)))
        video = max(VIDEO_FADE_RANGE[0], min(VIDEO_FADE_RANGE[1], ed.settings.value("export/loop_video_fade_ms", VIDEO_FADE_MS, type=int)))
        return LoopSpec.for_range(start, end, first, last, audio_fade_ms=audio, video_fade_ms=video)

    def current(self):
        ed = self.ed
        if ed.project is None or not ed.project.lyrics.lines:
            return None
        start, end = clip_range(ed.project.lyrics, ed.first, ed.last, 0)
        return self.spec(start, self.adjusted_end if self.adjusted_end is not None else end,
                         ed.first, ed.last, full=ed.mode == "full")

    def local_audio(self):
        ed = self.ed
        return ed.project is not None and ed.project.audio_samples is not None and (
            ed.mode == "lyrics" or ed.cmb_source.currentData() == "local")

    def refresh(self, spec: LoopSpec | None):
        ed = self.ed
        visible = ed.project is not None and ed.settings.value("experimental/loop", False, type=bool)
        disabled = self.unavailable(ed.mode == "full")
        self.row.setVisible(visible)
        self.switch.setEnabled(not disabled)
        with blocked_signals(self.switch):
            self.switch.setChecked(self.requested)
        self.switch.setToolTip(lang.t(disabled or "loop.explanation"))
        self.copy.setVisible(spec is not None and ed.mode == "lyrics")
        self.adjust.setVisible(spec is not None and self.local_audio())
        self.adjust.setEnabled(not self.analyzing)
        self.inspect.setVisible(spec is not None)
        if spec is None:
            with blocked_signals(self.inspect):
                self.inspect.setChecked(False)
        self.duration.setText(fmt_ms(spec.duration_ms) if spec else "")
        note = disabled or ("loop.tiktok" if spec and ed.mode == "lyrics" else "loop.explanation")
        self.note.setText(lang.t(note))
        self.inspect.setToolTip(lang.t("loop.inspectSpotify" if ed.mode == "song" and ed.cmb_source.currentData() == "spotify" else "loop.inspectTip"))
        show_proposal = spec is not None and (self.analyzing or self.proposal is not None)
        self.proposal_text.setVisible(show_proposal)
        self.dismiss.setVisible(show_proposal)
        self.accept.setVisible(show_proposal and self.proposal is not None)
        if show_proposal:
            if self.proposal is None:
                self.proposal_text.setText(lang.t("loop.analyzing"))
            else:
                duration = LoopSpec.for_range(spec.start_ms, self.proposal, spec.first, spec.last).duration_ms
                self.proposal_text.setText(lang.t("loop.proposed", time=fmt_ms(duration)))

    def copy_duration(self):
        if spec := self.current():
            QApplication.clipboard().setText(str(round(spec.duration_ms)))
            self.ed.notify(lang.t("loop.copied"), "ok")

    def suggest(self):
        ed = self.ed
        spec = self.current()
        if spec is None or not self.local_audio():
            return
        project = ed.project
        samples = project.audio_samples
        offset_ms = project.audio_offset_ms
        _, minimum = clip_range(project.lyrics, spec.first, spec.last, 0)
        self.invalidate_proposal(refresh=False)
        token = self._analysis_token

        def current():
            return (token == self._analysis_token and project is ed.project and samples is project.audio_samples
                    and offset_ms == project.audio_offset_ms and self.current() == spec and self.local_audio())

        def done(analysis):
            if token != self._analysis_token:
                return
            self.analyzing = False
            if current():
                self.proposal = suggest_bar_end(spec.start_ms, minimum, spec.end_ms, analysis.beats_ms,
                                                analysis.beat_period_ms, offset_ms, len(samples) * 1000 / local.RATE)
                if self.proposal is None:
                    ed.notify(lang.t("loop.noProposal"), "info")
            ed.refresh_loop()

        def failed(error):
            if token == self._analysis_token:
                self.invalidate_proposal()
                ed.notify(lang.t("loop.analysisError", error=str(error)), "error")

        if cached := cached_audio_analysis(samples, local.RATE):
            done(cached)
        else:
            self.analyzing = True
            ed.refresh_loop()
            run_async(lambda: analyze_audio(samples, local.RATE), done, failed)

    def accept_proposal(self):
        if self.proposal is not None and self.current() is not None and self.local_audio():
            self.adjusted_end = self.proposal
            self.invalidate_proposal()
