"""Anzuelo en el Editor: sugerir los mejores rangos de versos para un clip, previsualizarlos y encolarlos (incluidas variantes)."""
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from marea.async_util import run_async
from marea.hooks.analyze import Suggestion, load_weights, suggest
from marea.presets.store import PresetStore
from marea.ui import lang, theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.editor.wordfx_controller import WordFXWatcher
from marea.ui.widgets import fmt_ms, make_button

DURATIONS = (15, 30, 60)
CONFIDENCE_KEYS = {"alta": "hooks.confidence.high", "media": "hooks.confidence.medium", "baja": "hooks.confidence.low"}
CUSTOM_DEFAULT = 45


class _HoverList(QListWidget):
    """Lista que avisa cuando el cursor sale, para quitar el resaltado del rango en los versos."""
    left = Signal()

    def leaveEvent(self, e):
        self.left.emit()
        super().leaveEvent(e)


class HooksController:
    def __init__(self, editor, store: PresetStore | None = None):
        self.ed = editor
        self.store = store or PresetStore()
        self.results: dict[float, list[Suggestion]] = {}
        self.chip_target: float = 30.0
        self._busy = False
        self._weights = load_weights()
        self.with_audio = False

    # ---------------- construcción ----------------
    def build_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, theme.SPACE[3], 0, 0)
        lay.setSpacing(theme.SPACE[2])
        top = QHBoxLayout()
        self.btn_run = make_button("", "Primary", self.run)
        lang.bind(self.btn_run, "hooks.run")
        lang.bind(self.btn_run, "hooks.runTip", "setToolTip")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setVisible(False)
        top.addWidget(self.btn_run)
        top.addWidget(self.progress, 1)
        lay.addLayout(top)

        chips = QHBoxLayout()
        self.chip_group = QButtonGroup(w)
        self.chips: dict[float, QPushButton] = {}
        for sec in DURATIONS:
            self._add_chip(chips, float(sec), f"{sec} s")
        self.sp_custom = QSpinBox()
        self.sp_custom.setRange(5, 180)
        self.sp_custom.setValue(CUSTOM_DEFAULT)
        self.sp_custom.setSuffix(" s")
        lang.bind(self.sp_custom, "hooks.customTip", "setToolTip")
        self.sp_custom.valueChanged.connect(lambda _v: self._custom_changed())
        self._add_chip(chips, -1.0, "")
        lang.bind(self.chips[-1.0], "hooks.custom")
        chips.addWidget(self.sp_custom)
        chips.addStretch()
        lay.addLayout(chips)
        self.chips[30.0].setChecked(True)

        self.status = QLabel()
        self._set_status("hooks.status.pick")
        self.status.setObjectName("Caption")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.list = _HoverList()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.itemEntered.connect(lambda it: self._hover(it.data(Qt.UserRole)))
        self.list.left.connect(lambda: self._hover(None))
        self.list.itemClicked.connect(lambda it: self.apply(it.data(Qt.UserRole)))
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_queue = make_button("", "Ghost", self.enqueue_selected)
        lang.bind(self.btn_queue, "hooks.queue")
        lang.bind(self.btn_queue, "hooks.queueTip", "setToolTip")
        self.btn_variants = make_button("", "Ghost", self.enqueue_variants)
        lang.bind(self.btn_variants, "hooks.variants")
        lang.bind(self.btn_variants, "hooks.variantsTip", "setToolTip")
        row.addWidget(self.btn_queue)
        row.addWidget(self.btn_variants)
        row.addStretch()
        lay.addLayout(row)
        self.list.itemSelectionChanged.connect(self._sync_buttons)

        self._watch = WordFXWatcher(self._reload_weights, self.ed)
        self._watch.start(self._weight_files())
        self._sync_buttons()
        return w

    def _set_status(self, key: str = ""):
        """Status line: an i18n key (follows the language), or empty."""
        if key:
            lang.bind(self.status, key)
        else:
            lang.unbind(self.status)
            self.status.setText("")

    def _add_chip(self, layout, target: float, text: str):
        b = QPushButton(text)
        b.setObjectName("Chip")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _=False, t=target: self._chip_clicked(t))
        self.chip_group.addButton(b)
        layout.addWidget(b)
        self.chips[target] = b

    def _weight_files(self):
        from marea.hooks.analyze import BUNDLED_WEIGHTS
        from marea.paths import app_data
        return (BUNDLED_WEIGHTS, app_data() / "hooks" / "weights.json")

    def _reload_weights(self):
        self._weights = load_weights()
        self.with_audio = False
        if self.results and not self._busy:
            self.run()  # pesos nuevos: se recalculan las sugerencias

    # ---------------- cálculo ----------------
    def targets(self) -> tuple[float, ...]:
        return tuple(float(t) for t in DURATIONS) + (float(self.sp_custom.value()),)

    def run(self):
        ed = self.ed
        p = ed.project
        if not p or self._busy:
            return
        self._busy = True
        self.btn_run.setEnabled(False)
        self.progress.setVisible(True)
        self._set_status("hooks.status.analyzing")
        lyrics, title = p.lyrics, p.title
        samples, offset = p.audio_samples, float(p.audio_offset_ms)
        outro = float(ed.settings.value("export/outro_ms", 500))
        plan, weights, targets = ed._fx_plan, dict(self._weights), self.targets()
        fx_counts = None
        if plan is not None:
            fx_counts = [sum(1 for wi in range(len(l.text.split())) if plan.get(li, wi) is not None) for li, l in enumerate(lyrics.lines)]

        def work():
            analysis = None
            if samples is not None:
                from marea.audio import local
                from marea.audio.analysis import analyze_audio
                analysis = analyze_audio(samples, local.RATE)
            return suggest(lyrics, title, targets, analysis=analysis, audio_offset_ms=offset, fx_counts=fx_counts,
                           weights=weights, outro_ms=outro), analysis is not None

        pkg_attr("run_async", run_async)(work, lambda r: self._done(p, *r), self._failed)

    def _done(self, project, results, with_audio: bool):
        self._busy = False
        self.btn_run.setEnabled(True)
        self.progress.setVisible(False)
        if self.ed.project is not project:
            return  # cambió de proyecto mientras se calculaba
        self.results, self.with_audio = results, with_audio
        self._set_status("" if with_audio else "hooks.status.noAudio")
        self.show_results()

    def _failed(self, e):
        self._busy = False
        self.btn_run.setEnabled(True)
        self.progress.setVisible(False)
        self._set_status()
        self.ed.notify(lang.t("hooks.failed", error=e), "error")

    # ---------------- lista ----------------
    def _chip_clicked(self, target: float):
        self.chip_target = float(self.sp_custom.value()) if target < 0 else target
        self.show_results()

    def _custom_changed(self):
        if self.chips[-1.0].isChecked():
            self.chip_target = float(self.sp_custom.value())
            if self.chip_target in self.results:
                self.show_results()

    def show_results(self):
        self.list.clear()
        items = self.results.get(self.chip_target, [])
        if not items and self.results:
            self._set_status("hooks.status.recalc")
        for s in items:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, s)
            card = self._card(s)
            it.setSizeHint(QSize(100, card.sizeHint().height() + 6))
            self.list.addItem(it)
            self.list.setItemWidget(it, card)
        self._sync_buttons()

    def _card(self, s: Suggestion) -> QWidget:
        lines = self.ed.project.lyrics.lines
        w = QWidget()
        w.setAttribute(Qt.WA_TransparentForMouseEvents)  # el clic y el hover los maneja la lista
        v = QVBoxLayout(w)
        v.setContentsMargins(theme.SPACE[2], 2, theme.SPACE[2], 2)
        v.setSpacing(0)
        head = QLabel()
        lang.bind(head, "hooks.cardHead", first=s.first + 1, last=s.last + 1,
                  start=fmt_ms(lines[s.first].start), end=fmt_ms(lines[s.last].end))
        head.setObjectName("Body")
        sub = QLabel()
        lang.bind_fn(sub, "setText", lambda: sub.setText(lang.t(
            "hooks.cardSub", seconds=f"{s.duration_ms / 1000:.0f}", score=f"{s.score:.0f}",
            confidence=lang.t(CONFIDENCE_KEYS[s.confidence]) if s.confidence in CONFIDENCE_KEYS else s.confidence)))
        sub.setObjectName("Caption")
        why = QLabel(s.explanation)
        why.setObjectName("Muted")
        why.setWordWrap(True)
        for lb in (head, sub, why):
            v.addWidget(lb)
        return w

    def _hover(self, s: Suggestion | None):
        self.ed.set_hover_range((s.first, s.last) if s else None)

    def apply(self, s: Suggestion):
        self.ed.select_range(s.first, s.last)

    def selected(self) -> list[Suggestion]:
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    def _sync_buttons(self):
        have = bool(self.list.selectedItems()) and self.ed.project is not None
        self.btn_queue.setEnabled(have)
        self.btn_variants.setEnabled(have)

    # ---------------- cola ----------------
    def enqueue_selected(self):
        for s in self.selected():
            self.ed.enqueue_range(s.first, s.last, s.start_ms, s.end_ms)

    def pick_presets(self):
        """Corrientes para las variantes (diálogo con una casilla por Corriente); [] si se cancela."""
        infos = self.store.list()
        dlg = QDialog(self.ed)
        dlg.setWindowTitle(lang.t("hooks.variantsTitle"))
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(lang.t("hooks.variantsLabel")))
        boxes = []
        for info in infos:
            cb = QCheckBox(info.name)
            lay.addWidget(cb)
            boxes.append((cb, info))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        self._dialog = dlg
        if dlg.exec() != QDialog.Accepted:
            return []
        return [info for cb, info in boxes if cb.isChecked()]

    def enqueue_variants(self):
        sel = self.selected()
        if not sel:
            return
        presets = self.pick_presets()
        for s in sel:
            for info in presets:
                self.ed.enqueue_range(s.first, s.last, s.start_ms, s.end_ms, look_preset=info)
        if presets:
            self.ed.notify(lang.t("hooks.queued", count=len(sel) * len(presets)), "ok")

    def reset(self):
        """Proyecto nuevo: las sugerencias anteriores ya no aplican."""
        self.results = {}
        if hasattr(self, "list"):
            self.list.clear()
            self._set_status("hooks.status.pick")
            self._sync_buttons()
        self.ed.set_hover_range(None) if hasattr(self.ed, "list") else None
