"""Lab review window and prerequisite helpers."""
import dataclasses
import hashlib
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from marea.async_util import run_async
from marea.audio import local
from marea.i18n import t
from marea.lab import alignment as A
from marea.lab import env
from marea.lab.align_job import apply_result, format_draft_line, from_text_result, request_layers, text_request_lines
from marea.lab.player import Player
from marea.lab.session import Session
from marea.lab.ttml_in import from_ttml
from marea.paths import app_data, reveal_in_folder
from marea.sysutil import find_ffmpeg, is_local_path, safe_filename
from marea.ui import lang, theme
from marea.ui.lab._pkg import pkg_attr
from marea.ui.lab.prepare_view import build_prepare_page
from marea.ui.lab.runner import Runner
from marea.ui.lab.settings_panel import COMPONENTS
from marea.ui.lab.timeline import TimelineWidget
from marea.ui.widgets import make_button

FLAG = "experimental/align"


def _btn(key: str, kind: str, handler):
    """make_button whose text follows the UI language."""
    b = make_button("", kind, handler)
    lang.bind(b, key)
    return b


def enabled(settings) -> bool:
    return settings.value(FLAG, False, type=bool)


def lab_root(settings) -> Path:
    return env.lab_root(settings)


def layer_problem(root: Path) -> str | None:
    """Describe missing optional layer assets without loading models or using the network."""
    missing = []
    if not env.separation_installed(root):
        missing.append(lang.t("lab.layerMissing.package"))
    for key, filename in (("lab.layerMissing.vocalModel", env.SEPARATION_MODEL), ("lab.layerMissing.karaokeModel", env.KARAOKE_MODEL)):
        if not env.separator_model_ready(root, filename):
            missing.append(lang.t(key, filename=filename))
    if missing:
        return lang.t("lab.layerMissing.summary", items=", ".join(missing), folder=root / "separator-models")
    return None


def prerequisites(root: Path, *, lang: str, separate: bool, audio: Path | None, asr: bool = False, karaoke: bool = False) -> str | None:
    """Lo que falta para poder alinear/transcribir, en palabras (None = todo listo). Solo mira el disco."""
    if audio is None or not audio.exists():
        return t("lab.prereq.audio")
    if not env.is_installed(root):
        return t("lab.prereq.env")
    if not lang and not asr:
        return t("lab.prereq.lang")
    for _, kind, ident, _cmd, params, _size in COMPONENTS:
        wanted = (kind == "hf" and params.get("lang") == lang and not asr) or (asr and ident.endswith("large-v3-turbo"))
        if wanted and not env.hf_model_ready(root, ident):
            return t("lab.prereq.modelAsr") if asr else t("lab.prereq.modelAlign", language=lang)
    if karaoke and (problem := layer_problem(root)):
        return problem + " " + t("lab.prereq.karaokeHint")
    if separate and not (env.separation_installed(root) and env.separator_model_ready(root)):
        return t("lab.prereq.separation")
    return None


def fmt(ms: float) -> str:
    return f"{int(ms // 60000)}:{ms % 60000 / 1000:05.2f}"


class LabReviewWindow(QWidget):
    """Prepara la alineación (Modo A con el TTML del proyecto, o Modo B con texto) y la revisa."""
    audio_changed = Signal()

    def __init__(self, editor, runner: Runner | None = None, player: Player | None = None):
        super().__init__(None, Qt.Window)
        self.setObjectName("LabWindow")
        lang.bind(self, "lab.window.title", "setWindowTitle")
        self.setWindowIcon(theme.app_icon())
        self.resize(1240, 800)
        self.editor, self.settings, self.notify = editor, editor.settings, editor.notify
        self.project = self._orig_project = editor.project
        self.root = lab_root(self.settings)
        runner_cls = pkg_attr("Runner", Runner)
        self.runner = runner or runner_cls(self.root, parent=self)
        self.player = player or Player(self)
        self.session: Session | None = None
        self.skeleton = None
        self._original = None
        self._samples = None
        self._selected = ""
        self._realign_id = ""
        self._from_asr = False
        self._draft = []
        self._draft_overlaps = []
        self._detect_source()

        if callable(getattr(editor, "_refresh_audio_tab", None)):
            self.audio_changed.connect(editor._refresh_audio_tab)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE[5], theme.SPACE[4], theme.SPACE[5], theme.SPACE[4])
        root.setSpacing(theme.SPACE[3])
        head = QHBoxLayout()
        title = QLabel()
        lang.bind(title, "lab.title")
        title.setObjectName("H1")
        tag = QLabel()
        lang.bind(tag, "lab.experimental")
        tag.setObjectName("Caption")
        self.btn_prepare = _btn("lab.prepareBack", "Ghost", lambda: self.pages.setCurrentIndex(0))
        self.btn_prepare.setVisible(False)
        head.addWidget(title)
        head.addWidget(tag, 0, Qt.AlignBottom)
        head.addStretch()
        head.addWidget(self.btn_prepare)
        root.addLayout(head)

        self.pages = QStackedWidget()
        self.pages.addWidget(build_prepare_page(self))
        self.pages.addWidget(self._build_review())
        root.addWidget(self.pages, 1)
        lang.signals.changed.connect(self._refresh_layers)

        self.lb_progress = QLabel()
        self.lb_progress.setObjectName("Caption")
        self.lb_progress.setWordWrap(True)
        root.addWidget(self.lb_progress)

        self.runner.stage.connect(self._progress)
        self.runner.done.connect(self._on_done)
        self.runner.failed.connect(self._on_failed)
        self.runner.cancelled.connect(self._on_cancelled)
        self.player.position.connect(self.timeline.set_playhead)
        self.player.finished.connect(lambda: self.timeline.set_playhead(None))
        theme.signals.changed.connect(self._palette)
        self._palette()
        self._refresh_audio()

    # ---- construcción ----
    def _palette(self):
        pal = self.palette()
        pal.setColor(QPalette.Window, theme.qc(theme.current.bg))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

    def _detect_source(self):
        src = getattr(self.project, "source", None)
        self.mode = "text"
        if src and Path(src).suffix.lower() in (".ttml", ".xml") and Path(src).exists():
            data = Path(src).read_bytes()
            skeleton = from_ttml(data, 0)
            if request_layers(skeleton):
                skeleton.source_lyrics = {"kind": "ttml", "path_hint": str(src), "sha256": hashlib.sha256(data).hexdigest()}
                self.mode, self.skeleton, self._original = "ttml", skeleton, data

    def _build_review(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE[4])
        left = QVBoxLayout()
        self.timeline = TimelineWidget()
        self.timeline.unit_selected.connect(self._select)
        self.timeline.unit_edited.connect(self._dragged)
        self.timeline.play_requested.connect(lambda ms: self._play(ms, ms + 8000))
        hint = QLabel()
        lang.bind(hint, "lab.review.timelineHint")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setWidget(self.timeline)
        self.timeline_scroll.setFrameShape(QFrame.NoFrame)
        left.addWidget(self.timeline_scroll, 1)
        left.addWidget(hint)

        bottom = QHBoxLayout()
        lb = QLabel()
        lang.bind(lb, "lab.review.globalOffset")
        lb.setObjectName("Muted")
        self.sp_offset = QSpinBox()
        self.sp_offset.setRange(-20000, 20000)
        self.sp_offset.setSingleStep(10)
        self.sp_offset.setSuffix(" ms")
        self.sp_offset.editingFinished.connect(self._offset_edited)
        self.lb_offset = QLabel()
        self.lb_offset.setObjectName("Caption")
        self.btn_undo = _btn("lab.review.undo", "Ghost", self._undo)
        self.btn_preview = _btn("lab.review.preview", "Ghost", self._preview)
        self.btn_back = _btn("lab.review.backToOriginal", "Ghost", self._back)
        self.btn_approve = _btn("lab.review.approve", "Primary", self._approve)
        bottom.addWidget(lb)
        bottom.addWidget(self.sp_offset)
        bottom.addWidget(self.lb_offset, 1)
        for b in (self.btn_undo, self.btn_preview, self.btn_back, self.btn_approve):
            bottom.addWidget(b)
        left.addLayout(bottom)
        lay.addLayout(left, 1)

        right = QFrame()
        right.setObjectName("Card")
        right.setMinimumWidth(320)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(theme.SPACE[3], theme.SPACE[3], theme.SPACE[3], theme.SPACE[3])
        self.lb_review = QLabel()
        self.lb_review.setObjectName("H2")
        self.review_list = QListWidget()
        self.review_list.itemClicked.connect(lambda it: self._select(it.data(Qt.UserRole), center=True))
        self.review_list.itemDoubleClicked.connect(lambda it: (self._select(it.data(Qt.UserRole), center=True), self._play_selected()))
        rl.addWidget(self.lb_review)
        rl.addWidget(self.review_list, 1)
        self.lb_sel = QLabel()
        lang.bind(self.lb_sel, "lab.review.selectUnit")
        self.lb_sel.setObjectName("Body")
        self.lb_sel.setWordWrap(True)
        rl.addWidget(self.lb_sel)

        row = QHBoxLayout()
        self.sp_start, self.sp_end = QSpinBox(), QSpinBox()
        for sp in (self.sp_start, self.sp_end):
            sp.setRange(0, 3_600_000)
            sp.setSingleStep(10)
            sp.setSuffix(" ms")
            row.addWidget(sp)
        rl.addLayout(row)

        row = QHBoxLayout()
        self.btn_apply = _btn("lab.review.applyTimes", "Primary", self._apply_times)
        self.btn_play = _btn("lab.review.listen", "Ghost", self._play_selected)
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_play)
        rl.addLayout(row)

        self.ed_line = QLineEdit()
        lang.bind(self.ed_line, "lab.review.linePlaceholder", "setPlaceholderText")
        self.btn_realign = _btn("lab.review.realign", "Ghost", self._realign)
        rl.addWidget(self.ed_line)
        self.btn_text = _btn("lab.review.replaceLine", "Ghost", self._apply_text)
        rl.addWidget(self.btn_text)

        self.ed_unit = QLineEdit()
        lang.bind(self.ed_unit, "lab.review.unitPlaceholder", "setPlaceholderText")
        lang.bind(self.ed_unit, "lab.review.unitAccessible", "setAccessibleName")
        self.btn_unit_text = _btn("lab.review.correctUnit", "Ghost", self._apply_unit_text)
        rl.addWidget(self.ed_unit)
        rl.addWidget(self.btn_unit_text)

        self.cb_role = QComboBox()
        for role in A.ROLES:
            self.cb_role.addItem(role, role)
        lang.bind(self.cb_role, "lab.review.roleAccessible", "setAccessibleName")
        self.ed_agent = QLineEdit()
        lang.bind(self.ed_agent, "lab.review.agentPlaceholder", "setPlaceholderText")
        lang.bind(self.ed_agent, "lab.review.agentAccessible", "setAccessibleName")
        rl.addWidget(self.cb_role)
        rl.addWidget(self.ed_agent)
        self.btn_identity = _btn("lab.review.applyIdentity", "Ghost", self._apply_identity)
        rl.addWidget(self.btn_identity)

        row = QHBoxLayout()
        self.btn_accept = _btn("lab.review.accept", "Ghost", lambda: self._decision(True))
        self.btn_reject = _btn("lab.review.reject", "Ghost", lambda: self._decision(False))
        row.addWidget(self.btn_accept)
        row.addWidget(self.btn_reject)
        rl.addLayout(row)

        self.btn_add = _btn("lab.review.addPerformance", "Ghost", self._add_performance)
        self.btn_split = _btn("lab.review.split", "Ghost", self._split_performance)
        rl.addWidget(self.btn_add)
        rl.addWidget(self.btn_split)
        self.btn_save = _btn("lab.review.saveJson", "Ghost", self._save_review)
        rl.addWidget(self.btn_save)
        rl.addWidget(self.btn_realign)
        self._set_unit_controls(False)

        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setWidget(right)
        self.review_scroll.setFixedWidth(365)
        self.review_scroll.setFrameShape(QFrame.NoFrame)
        lay.addWidget(self.review_scroll)
        return page

    # ---- audio ----
    def _refresh_layers(self):
        problem = layer_problem(self.root)
        self.cb_layers.setEnabled(problem is None or self.cb_layers.isChecked())
        self.cb_layers.setToolTip(problem or lang.t("lab.layers.available"))
        self.lb_layers_status.setText(
            lang.t("lab.layers.incomplete", folder=self.root / "separator-models")
            if problem else ""
        )
        self.lb_layers_status.setToolTip(problem or "")
        self.lb_layers_status.setVisible(problem is not None)
        self.btn_layer_models.setVisible(problem is not None)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow() and hasattr(self, "lb_layers_status"):
            self._refresh_layers()

    def _refresh_audio(self):
        f = getattr(self.project, "audio_file", None)
        if f:
            lang.bind(self.lb_audio, "lab.audio.file", name=Path(f).name)
        else:
            lang.bind(self.lb_audio, "lab.audio.none")
        self._samples = getattr(self.project, "audio_samples", None)

    def _choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, lang.t("lab.audio.chooseTitle"), "", lang.t("lab.audio.chooseFilter"))
        if not path:
            return
        self._progress(lang.t("lab.progress.readingAudio"))

        def done(samples):
            self.project.audio_file, self.project.audio_samples = Path(path), samples
            self._refresh_audio()
            self._progress("")
            self.audio_changed.emit()

        pkg_attr("run_async", run_async)(lambda: local.decode(path), done, lambda e: self._progress(lang.t("lab.progress.cannotReadChosen", error=e)))

    def _ensure_samples(self, then):
        if self._samples is not None:
            then()
            return
        f = getattr(self.project, "audio_file", None)
        if not f:
            return

        def got(s):
            self._samples = s
            self.timeline.set_samples(s)
            then()

        pkg_attr("run_async", run_async)(lambda: local.decode(str(f)), got, lambda e: self._progress(lang.t("lab.progress.cannotRead", error=e)))

    # ---- progreso ----
    def _progress(self, text: str):
        self.lb_progress.setText(text)

    def _busy(self, on: bool):
        for b in (self.btn_align, self.btn_asr, self.btn_open, self.btn_audio):
            b.setEnabled(not on)
        self.btn_cancel.setVisible(on)

    def _cancel(self):
        self.runner.cancel()
        self._progress(lang.t("lab.common.cancelling"))

    # ---- preparar / alinear ----
    def _lang_code(self) -> str:
        return self.cb_lang.currentData() or ""

    def _sep_model(self) -> str:
        return env.SEPARATION_MODEL if self.cb_sep.isChecked() else ""

    def _transcribe(self):
        prereq_fn = pkg_attr("prerequisites", prerequisites)
        problem = prereq_fn(
            self.root,
            lang=self._lang_code(),
            separate=self.cb_sep.isChecked(),
            karaoke=self.cb_layers.isChecked(),
            audio=getattr(self.project, "audio_file", None),
            asr=True,
        )
        if problem:
            self._progress(problem)
            return
        self._busy(True)
        ffmpeg_fn = pkg_attr("find_ffmpeg", find_ffmpeg)
        self.runner.ask(
            "asr",
            audio=str(self.project.audio_file),
            ffmpeg=ffmpeg_fn(),
            model=env.ASR_MODEL,
            lang=self._lang_code(),
            separate=self._sep_model(),
            karaoke=env.KARAOKE_MODEL if self.cb_layers.isChecked() else "",
        )

    def _align(self):
        lang = self._lang_code() or (self.skeleton.language[:2] if self.skeleton else "")
        prereq_fn = pkg_attr("prerequisites", prerequisites)
        problem = prereq_fn(
            self.root,
            lang=lang,
            separate=self.cb_sep.isChecked(),
            karaoke=self.cb_layers.isChecked(),
            audio=getattr(self.project, "audio_file", None),
        )
        if problem:
            self._progress(problem)
            return
        if self.mode == "ttml":
            lines = request_layers(self.skeleton)
        else:
            try:
                lines = text_request_lines(self.ed_text.toPlainText().splitlines(), self._draft)
            except ValueError as e:
                self._progress(str(e))
                return
            if not lines:
                self._progress(t("lab.progress.needLyrics"))
                return
        self._busy(True)
        self._realign_id = ""
        ffmpeg_fn = pkg_attr("find_ffmpeg", find_ffmpeg)
        self.runner.ask(
            "align",
            audio=str(self.project.audio_file),
            ffmpeg=ffmpeg_fn(),
            lang=lang,
            lines=lines,
            separate=self._sep_model(),
            karaoke=env.KARAOKE_MODEL if self.cb_layers.isChecked() else "",
        )

    def _on_done(self, cmd: str, data: dict):
        self._busy(False)
        if cmd == "asr":
            self._from_asr = True
            self._draft = data["lines"]
            self._draft_overlaps = data.get("overlaps", [])
            self.ed_text.setPlainText("\n".join(format_draft_line(l) for l in self._draft))
            i = self.cb_lang.findData(data["language"])
            if not self._lang_code() and i >= 0:
                self.cb_lang.setCurrentIndex(i)
            sus = sum(l.get("suspect", False) for l in data["lines"])
            self._progress(
                lang.t("lab.progress.draft", language=data["language"], count=len(data["lines"]))
                + (lang.t("lab.progress.draftSuspect", count=sus) if sus else "")
                + lang.t("lab.progress.draftReview")
            )
            if self._draft_overlaps:
                self._progress(lang.t("lab.progress.overlaps", count=len(self._draft_overlaps)))
        elif cmd == "align" and self._realign_id:
            errors = self.session.apply_realign(self._realign_id, data)
            self._progress(lang.t("lab.progress.realigned") if not errors else lang.t("lab.progress.realignFailed", error=errors[0]))
            self._after_edit()
        elif cmd == "align":
            if self.mode == "ttml":
                al, original = apply_result(self.skeleton, data), self._original
            else:
                al, original = from_text_result(data, "asr" if self._from_asr else "text"), None
                if self._draft_overlaps:
                    al.source_lyrics["overlaps"] = self._draft_overlaps
            self.session = Session(al, original)
            self._progress(lang.t("lab.progress.aligned", device=data["device"]["device"], count=len(self.session.review_ids())))
            self._populate()

    def _on_failed(self, _cmd: str, message: str):
        self._busy(False)
        self._progress(message or lang.t("lab.progress.failedGeneric"))

    def _on_cancelled(self):
        self._busy(False)
        self._progress(lang.t("lab.common.cancelled"))

    # ---- revisar ----
    def _populate(self):
        self.timeline.set_session(self.session)
        self._ensure_samples(lambda: self.timeline.set_samples(self._samples))
        go = self.session.alignment.global_offset
        self.sp_offset.setValue(int(go.get("ms", 0)))
        method, confidence = go.get("method", ""), f"{go.get('confidence', 0):.0%}"
        unreliable = method.endswith("rejected")
        lang.bind_fn(
            self.lb_offset,
            "setText",
            lambda: self.lb_offset.setText(
                lang.t("lab.review.offsetInfo", method=method, confidence=confidence)
                + (lang.t("lab.review.offsetUnreliable") if unreliable else "")
            ),
        )
        self._selected = ""
        self._set_unit_controls(False)
        self.btn_prepare.setVisible(True)
        self.pages.setCurrentIndex(1)
        self._fill_review()

    def _fill_review(self):
        self.review_list.clear()
        ids = self.session.review_ids()
        total = sum(len(w.syllables or [w]) for l in self.session.alignment.lines for w in l.words)
        lang.bind(self.lb_review, "lab.review.needsReview", count=len(ids), total=total)
        for uid in ids:
            _kind, unit, _line = self.session.find(uid)
            it = QListWidgetItem(lang.t("lab.review.itemLabel", time=fmt(unit.start), text=unit.text, confidence=f"{unit.confidence:.2f}"))
            it.setData(Qt.UserRole, uid)
            self.review_list.addItem(it)
        self.timeline.refresh()
        pending = self.session.pending_ids()
        self.btn_approve.setEnabled(not pending)
        if pending:
            lang.bind(self.btn_approve, "lab.review.approveBlocked", "setToolTip")
        else:
            lang.unbind(self.btn_approve, "setToolTip")
            self.btn_approve.setToolTip("")

    def _set_unit_controls(self, on: bool):
        for w in (self.sp_start, self.sp_end, self.btn_apply, self.btn_play, self.btn_realign):
            w.setEnabled(on)
        self.ed_line.setEnabled(on and bool(self.session and self.session.text_editable))
        for widget in (
            self.btn_text,
            self.ed_unit,
            self.btn_unit_text,
            self.cb_role,
            self.ed_agent,
            self.btn_identity,
            self.btn_accept,
            self.btn_reject,
            self.btn_split,
        ):
            widget.setEnabled(on)

    def _select(self, uid: str, center: bool = False):
        self._selected = uid
        self.timeline.set_selected(uid)
        found = self.session.find(uid) if (uid and self.session) else None
        self._set_unit_controls(found is not None)
        if not found:
            lang.bind(self.lb_sel, "lab.review.selectUnit")
            return
        kind, unit, line = found
        names = {"line": "lab.review.kindLine", "word": "lab.review.kindWord", "syllable": "lab.review.kindSyllable"}
        info = dict(
            kind=kind, text=unit.text, confidence=f"{unit.confidence:.2f}", approximate=unit.approximate, pinned=unit.manual_override,
            role=line.role, agent=line.agent, state=line.review_state,
            uncalibrated=not line.role_reviewed and "ttml_p_index" not in line.ref,
        )

        def selection_text():
            unknown_agent = lang.t("lab.review.unknownAgent")
            return (
                lang.t("lab.review.unitSummary", kind=lang.t(names[info["kind"]]), text=info["text"], confidence=info["confidence"])
                + (lang.t("lab.review.approximate") if info["approximate"] else "")
                + (lang.t("lab.review.pinned") if info["pinned"] else "")
                + f" / {info['role']} / {info['agent'] or unknown_agent} / {info['state']}"
                + (lang.t("lab.review.uncalibrated") if info["uncalibrated"] else "")
            )

        lang.bind_fn(self.lb_sel, "setText", lambda: self.lb_sel.setText(selection_text()))
        self.sp_start.setValue(int(unit.start))
        self.sp_end.setValue(int(unit.end))
        self.ed_line.setText(line.text)
        self.ed_unit.setText(unit.text if kind != "line" else "")
        self.ed_unit.setEnabled(kind != "line")
        self.btn_unit_text.setEnabled(kind != "line")
        self.cb_role.setCurrentIndex(self.cb_role.findData(line.role))
        self.ed_agent.setText(line.agent)
        self.btn_accept.setEnabled(line.review_state == "pending" or not line.role_reviewed)
        self.btn_split.setEnabled(kind == "word" and line.words.index(unit) > 0)
        if center:
            self.timeline.center_on((unit.start + unit.end) / 2)

    def _after_edit(self):
        self.timeline.refresh()
        self._fill_review()
        if self._selected and self.session.find(self._selected):
            self._select(self._selected)
        else:
            self._select("")

    def _apply_text(self):
        line = self.session.find(self._selected)[2]
        errors = self.session.set_text(line.id, self.ed_line.text())
        self._progress(errors[0] if errors else lang.t("lab.progress.textCorrected"))
        self._after_edit()

    def _apply_unit_text(self):
        errors = self.session.set_text(self._selected, self.ed_unit.text())
        self._progress(errors[0] if errors else lang.t("lab.progress.unitCorrected"))
        self._after_edit()

    def _apply_identity(self):
        line = self.session.find(self._selected)[2]
        errors = self.session.set_identity(line.id, self.cb_role.currentData(), self.ed_agent.text().strip())
        self._progress(errors[0] if errors else lang.t("lab.progress.identityUpdated"))
        self._after_edit()

    def _decision(self, accept: bool):
        line = self.session.find(self._selected)[2]
        errors = self.session.accept(line.id) if accept else self.session.reject(line.id)
        self._progress(errors[0] if errors else lang.t("lab.progress.accepted") if accept else lang.t("lab.progress.rejected"))
        self._after_edit()

    def _add_performance(self):
        text, ok = QInputDialog.getText(self, lang.t("lab.dialog.addTitle"), lang.t("lab.dialog.addText"))
        if not ok or not text.strip():
            return
        duration = self.session.alignment.duration_ms
        start, ok = QInputDialog.getInt(self, lang.t("lab.dialog.startTitle"), lang.t("lab.dialog.startLabel"), min(self.sp_start.value(), duration - 1), 0, duration - 1)
        if not ok:
            return
        end, ok = QInputDialog.getInt(self, lang.t("lab.dialog.endTitle"), lang.t("lab.dialog.endLabel"), min(duration, max(start + 1, self.sp_end.value())), start + 1, duration)
        if ok:
            errors = self.session.add_performance(text, start, end)
            self._progress(errors[0] if errors else lang.t("lab.progress.performanceAdded"))
            self._after_edit()

    def _split_performance(self):
        kind, unit, line = self.session.find(self._selected)
        if kind == "word":
            errors = self.session.split_performance(line.id, line.words.index(unit))
            self._progress(errors[0] if errors else lang.t("lab.progress.performanceSplit"))
            self._after_edit()

    def _save_review(self):
        path, _ = QFileDialog.getSaveFileName(self, lang.t("lab.dialog.saveTitle"), "review.alignment.json", lang.t("lab.dialog.saveFilter"))
        if path:
            try:
                A.save(self.session.alignment, path)
            except OSError as e:
                self._progress(lang.t("lab.progress.saveFailed", error=e))

    def _apply_times(self):
        errors = self.session.set_times(self._selected, self.sp_start.value(), self.sp_end.value())
        if errors:
            self._progress(lang.t("lab.progress.notApplied", error=errors[0]))
            self._select(self._selected)
        else:
            self._progress("")
            self._after_edit()

    def _dragged(self, uid: str, start: int, end: int):
        errors = self.session.set_times(uid, start, end)
        self._progress(lang.t("lab.progress.notApplied", error=errors[0]) if errors else "")
        self._after_edit()

    def _undo(self):
        if self.session and self.session.undo():
            self.sp_offset.setValue(int(self.session.alignment.global_offset.get("ms", 0)))
            self._after_edit()

    def _offset_edited(self):
        if self.session and self.sp_offset.value() != int(self.session.alignment.global_offset.get("ms", 0)):
            self.session.set_offset(self.sp_offset.value())
            lang.bind(self.lb_offset, "lab.review.offsetManual")

    # ---- escuchar ----
    def _play(self, start_ms: float, end_ms: float):
        def go():
            try:
                self.player.play(self._samples, start_ms, end_ms)
            except OSError:
                self._progress(lang.t("lab.progress.noOutputDevice"))

        self._ensure_samples(go)

    def _play_selected(self):
        found = self.session.find(self._selected) if self.session else None
        if found:
            self._play(max(0, found[1].start - 300), found[1].end + 500)

    # ---- recalcular ----
    def _realign(self):
        _, _unit, line = self.session.find(self._selected)
        text = self.ed_line.text().strip()
        if text and text != line.text:
            errors = self.session.set_text(line.id, text)
            if errors:
                self._progress(errors[0])
                return
            line = self.session.find(line.id)[1]
        try:
            req = self.session.realign_request(line.id)
        except ValueError as e:
            self._progress(str(e))
            return
        src = self.session.alignment.pipeline.get("source", "mix")
        self._realign_id = line.id
        self._busy(True)
        ffmpeg_fn = pkg_attr("find_ffmpeg", find_ffmpeg)
        self.runner.ask(
            "align",
            audio=str(self.project.audio_file),
            ffmpeg=ffmpeg_fn(),
            lang=self.session.alignment.language,
            separate=self.session.alignment.pipeline.get("separate", src.split(":", 1)[1] if src.startswith("vocals:") else ""),
            karaoke=self.session.alignment.pipeline.get("karaoke", ""),
            **req,
        )

    # ---- salida ----
    def _preview(self):
        try:
            lyrics = self.session.preview_lyrics()
        except A.AlignmentError as e:
            self._progress(lang.t("lab.progress.previewInvalid", error=e))
            return
        lyrics.provider_tag = "manual"
        lyrics.manual_override = True
        self.editor.set_project(dataclasses.replace(self._orig_project, lyrics=lyrics, audio_offset_ms=0.0))
        self.notify(lang.t("lab.notify.previewLoaded"), "info")

    def _back(self):
        self.editor.set_project(self._orig_project)
        self.notify(lang.t("lab.notify.originalRestored"), "info")

    def _approve(self):
        src = getattr(self._orig_project, "source", None)
        app_data_fn = pkg_attr("app_data", app_data)
        folder = Path(src).parent if src else app_data_fn() / "letras"
        name = safe_filename(Path(src).stem if src else self._orig_project.title or "", "letra") + ".aligned.ttml"
        try:
            ttml, _js = self.session.export(folder, name)
        except A.AlignmentError as e:
            self._progress(lang.t("lab.progress.notExported", error=e))
            return
        except OSError:
            ttml, _js = self.session.export(app_data_fn() / "letras", name)
        self.notify(
            lang.t("lab.notify.exported", name=ttml.name),
            "ok",
            (lang.t("lab.notify.openFolder"), lambda: reveal_in_folder(str(ttml))),
        )

    def _open_json(self):
        path, _ = QFileDialog.getOpenFileName(self, lang.t("lab.dialog.openTitle"), "", lang.t("lab.dialog.openFilter"))
        if path:
            self.load_alignment(path)

    def load_alignment(self, path):
        try:
            al = A.load(path)
        except (A.AlignmentError, OSError, ValueError) as e:
            self._progress(lang.t("lab.progress.cannotOpenAlignment", error=e))
            return
        original = None
        hint = al.source_lyrics.get("path_hint")
        if al.source_lyrics.get("kind") == "ttml" and hint and is_local_path(hint) and Path(hint).exists():
            data = Path(hint).read_bytes()
            if hashlib.sha256(data).hexdigest() == al.source_lyrics.get("sha256"):
                original = data
            else:
                self._progress(lang.t("lab.progress.sourceChanged"))
        self.mode = "ttml" if original else "text"
        self._original = original
        self.session = Session(al, original)
        self._populate()

    def closeEvent(self, e):
        self.runner.close()
        self.player.close()
        super().closeEvent(e)


def open_review(editor) -> LabReviewWindow:
    """Abre (o trae al frente) la ventana de revisión del proyecto actual del Editor."""
    win = getattr(editor, "_lab_window", None)
    if win is None or not win.isVisible() or win.project is not editor.project:
        if win is not None:
            win.close()
        win = editor._lab_window = LabReviewWindow(editor)
    win.show()
    win.raise_()
    win.activateWindow()
    return win
