"""Perfil de ánimo de la canción (Semantic Word FX): tags y arco editables antes de grabar.

Solo se importa con la función activada. El cálculo vive en `marea.wordfx.profile`; aquí solo se edita:
- tags: botones marcables (vocabulario fijo);
- arco: barra de tiempo con un tramo por ánimo (clic para elegir; ánimo, inicio/fin, dividir y unir);
- notas: texto libre que sesga los tags al recalcular.
"""
from typing import Callable

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from marea.ui import lang, theme
from marea.ui.widgets import make_button
from marea.wordfx.profile import ARC_MOODS, TAGS, ArcSegment, SongProfile

LABELS = {  # tag / mood -> i18n key of its label
    "sad": "wordfxUi.profile.mood.sad", "nostalgic": "wordfxUi.profile.mood.nostalgic", "longing": "wordfxUi.profile.mood.longing",
    "anguished": "wordfxUi.profile.mood.anguished", "tender": "wordfxUi.profile.mood.tender",
    "hopeful": "wordfxUi.profile.mood.hopeful", "joyful": "wordfxUi.profile.mood.joyful",
    "euphoric": "wordfxUi.profile.mood.euphoric", "bittersweet": "wordfxUi.profile.mood.bittersweet",
    "tribute": "wordfxUi.profile.mood.tribute", "healing": "wordfxUi.profile.mood.healing", "numb": "wordfxUi.profile.mood.numb",
    "raw_vocals": "wordfxUi.profile.mood.rawVocals", "calm": "wordfxUi.profile.mood.calm"}
MIN_SEG_S = 1.0


def _mood_label(m: str) -> str:
    return lang.t(LABELS[m]) if m in LABELS else m


class ArcTimeline(QWidget):
    """Barra con un tramo de color por ánimo; el tramo elegido se resalta."""
    selected_changed = Signal(int)

    def __init__(self, arc: list[ArcSegment], duration: float, parent=None):
        super().__init__(parent)
        self.arc, self.duration, self.sel = arc, max(1.0, duration), 0
        self.setMinimumHeight(46)
        self.setCursor(Qt.PointingHandCursor)
        lang.signals.changed.connect(self.update)  # mood names are painted

    def set_arc(self, arc: list[ArcSegment], sel: int | None = None):
        self.arc = arc
        self.sel = min(self.sel if sel is None else sel, max(0, len(arc) - 1))
        self.update()
        self.selected_changed.emit(self.sel)

    def _x(self, t: float) -> float:
        return t / self.duration * self.width()

    def mousePressEvent(self, e):
        t = e.position().x() / max(1, self.width()) * self.duration
        for i, s in enumerate(self.arc):
            if s.start <= t < s.end:
                self.sel = i
                self.update()
                self.selected_changed.emit(i)
                return

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        p.fillRect(self.rect(), theme.qc(theme.current.hover))
        for i, s in enumerate(self.arc):
            r = QRectF(self._x(s.start), 4, max(2.0, self._x(s.end) - self._x(s.start)), h - 8)
            p.setBrush(theme.qc(theme.MOOD_COLORS.get(s.mood, theme.current.text_muted)))
            p.setPen(QPen(theme.qc(theme.current.text), 2) if i == self.sel else Qt.NoPen)
            p.drawRoundedRect(r, 5, 5)
            if r.width() > 54:
                p.setPen(QColor(0, 0, 0, 200))
                p.drawText(r, Qt.AlignCenter, _mood_label(s.mood))
        p.end()


class ProfileDialog(QDialog):
    """`recompute(notes) -> SongProfile` rehace el perfil automático (descarta lo editado, conserva las notas)."""

    def __init__(self, profile: SongProfile, duration_s: float, recompute: Callable[[str], SongProfile], title: str = "", parent=None):
        super().__init__(parent)
        lang.bind_fn(self, "setWindowTitle", lambda: self.setWindowTitle(
            lang.t("wordfxUi.profile.title") + (f" · {title}" if title else "")))
        self.setMinimumWidth(660)
        self._recompute = recompute
        self.duration = max(1.0, duration_s)
        self.intensity = profile.intensity
        self.track_id = profile.track_id
        lay = QVBoxLayout(self)
        hint = QLabel()
        lang.bind(hint, "wordfxUi.profile.hint")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        tags_label = QLabel()
        lang.bind(tags_label, "wordfxUi.profile.tags")
        lay.addWidget(tags_label)
        grid = QGridLayout()
        self.tag_buttons: dict[str, QPushButton] = {}
        for i, tag in enumerate(TAGS):
            b = make_button("", "Ghost")
            lang.bind_fn(b, "setText", lambda b=b, tag=tag: b.setText(_mood_label(tag)))
            b.setCheckable(True)
            b.setToolTip(tag)
            self.tag_buttons[tag] = b
            grid.addWidget(b, i // 5, i % 5)
        lay.addLayout(grid)

        arc_label = QLabel()
        lang.bind(arc_label, "wordfxUi.profile.arc")
        lay.addWidget(arc_label)
        self.timeline = ArcTimeline([], self.duration)
        self.timeline.selected_changed.connect(self._load_selected)
        lay.addWidget(self.timeline)
        row = QHBoxLayout()
        self.cmb_mood = QComboBox()
        lang.bind_items(self.cmb_mood, [(LABELS[m], m) for m in ARC_MOODS])
        self.sp_start, self.sp_end = QDoubleSpinBox(), QDoubleSpinBox()
        for sp in (self.sp_start, self.sp_end):
            sp.setRange(0.0, self.duration)
            sp.setDecimals(1)
            sp.setSuffix(" s")
        self.btn_split, self.btn_merge = QPushButton(), QPushButton()
        lang.bind(self.btn_split, "wordfxUi.profile.split")
        lang.bind(self.btn_merge, "wordfxUi.profile.merge")
        self.cmb_mood.setMinimumWidth(150)
        for b in (self.btn_split, self.btn_merge):
            b.setObjectName("Ghost")
            b.setMinimumWidth(150 if b is self.btn_merge else 100)
            b.setCursor(Qt.PointingHandCursor)
        for w in (self.cmb_mood, self.sp_start, self.sp_end, self.btn_split, self.btn_merge):
            row.addWidget(w)
        lay.addLayout(row)
        self.cmb_mood.activated.connect(self._mood_edited)
        self.sp_start.editingFinished.connect(self._bounds_edited)
        self.sp_end.editingFinished.connect(self._bounds_edited)
        self.btn_split.clicked.connect(self._split)
        self.btn_merge.clicked.connect(self._merge)

        notes_label = QLabel()
        lang.bind(notes_label, "wordfxUi.profile.notes")
        lay.addWidget(notes_label)
        self.notes = QPlainTextEdit(profile.notes)
        self.notes.setFixedHeight(64)
        lay.addWidget(self.notes)
        self.lb_info = QLabel()
        self.lb_info.setObjectName("Caption")
        lay.addWidget(self.lb_info)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.btn_recalc = btns.addButton("", QDialogButtonBox.ActionRole)
        lang.bind(self.btn_recalc, "wordfxUi.profile.recalc")
        lang.bind(btns.button(QDialogButtonBox.Save), "wordfxUi.profile.save")
        btns.button(QDialogButtonBox.Save).setObjectName("Primary")
        lang.bind(btns.button(QDialogButtonBox.Cancel), "wordfxUi.profile.cancel")
        for b in (self.btn_recalc, btns.button(QDialogButtonBox.Cancel)):
            b.setObjectName("Ghost")
        for b in btns.buttons():
            b.setCursor(Qt.PointingHandCursor)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self.btn_recalc.clicked.connect(self._recalculate)
        lay.addWidget(btns)
        self._fill(profile)

    # ---- datos ----
    def _fill(self, profile: SongProfile):
        for tag, b in self.tag_buttons.items():
            b.setChecked(tag in profile.tags)
        self.intensity = profile.intensity
        lang.bind(self.lb_info, "wordfxUi.profile.intensity", percent=round(profile.intensity * 100))
        self.timeline.set_arc([ArcSegment(s.start, s.end, s.mood) for s in profile.arc], 0)

    def result_profile(self) -> SongProfile:
        return SongProfile(self.track_id, [t for t, b in self.tag_buttons.items() if b.isChecked()],
                           [ArcSegment(s.start, s.end, s.mood) for s in self.timeline.arc], self.intensity,
                           self.notes.toPlainText().strip())

    def _recalculate(self):
        self._fill(self._recompute(self.notes.toPlainText().strip()))

    # ---- edición del arco ----
    def _seg(self) -> ArcSegment | None:
        a = self.timeline.arc
        return a[self.timeline.sel] if a else None

    def _load_selected(self, _=0):
        s = self._seg()
        for w in (self.cmb_mood, self.sp_start, self.sp_end, self.btn_split, self.btn_merge):
            w.setEnabled(s is not None)
        if s is None:
            return
        self.cmb_mood.setCurrentIndex(max(0, self.cmb_mood.findData(s.mood)))
        self.sp_start.setValue(s.start)
        self.sp_end.setValue(s.end)
        self.btn_merge.setEnabled(self.timeline.sel < len(self.timeline.arc) - 1)

    def _mood_edited(self):
        if s := self._seg():
            s.mood = self.cmb_mood.currentData()
            self.timeline.update()

    def _bounds_edited(self):
        """Mover el inicio o el fin arrastra al vecino para que el arco siga contiguo; nunca deja un tramo < MIN_SEG_S."""
        arc, i = self.timeline.arc, self.timeline.sel
        if not arc:
            return
        s = arc[i]
        lo = (arc[i - 1].start + MIN_SEG_S) if i else 0.0
        hi = (arc[i + 1].end - MIN_SEG_S) if i + 1 < len(arc) else self.duration
        start = min(max(self.sp_start.value(), lo), s.end - MIN_SEG_S) if i else min(self.sp_start.value(), s.end - MIN_SEG_S)
        end = max(min(self.sp_end.value(), hi), start + MIN_SEG_S)
        s.start, s.end = start, end
        if i:
            arc[i - 1].end = start
        if i + 1 < len(arc):
            arc[i + 1].start = end
        self._load_selected()
        self.timeline.update()

    def _split(self):
        arc, i = self.timeline.arc, self.timeline.sel
        s = self._seg()
        if s is None or s.end - s.start < 2 * MIN_SEG_S:
            return
        mid = (s.start + s.end) / 2
        arc.insert(i + 1, ArcSegment(mid, s.end, s.mood))
        s.end = mid
        self.timeline.set_arc(arc, i)

    def _merge(self):
        arc, i = self.timeline.arc, self.timeline.sel
        if i + 1 < len(arc):
            arc[i].end = arc[i + 1].end
            del arc[i + 1]
            self.timeline.set_arc(arc, i)
