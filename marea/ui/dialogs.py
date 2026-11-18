"""Native candidate selection dialogs for ambiguous track metadata and lyrics matches."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from marea.ui import lang, theme
from marea.ui.source_tag import paint_source_pill
from marea.ui.widgets import fmt_ms, make_button

CAND_ROLE = Qt.UserRole
ROW_H = 62


@dataclass
class CandidateItem:
    title: str
    subtitle: str
    badge: str
    badge_tag: str
    duration_ms: int | None
    payload: object


class _CandidateDelegate(QStyledItemDelegate):
    def sizeHint(self, option, _index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, opt, index):
        t = theme.current
        cand: CandidateItem = index.data(CAND_ROLE)
        if cand is None:
            return
        r = QRectF(opt.rect).adjusted(2, 2, -2, -2)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        selected = bool(opt.state & QStyle.State_Selected)
        hovered = bool(opt.state & QStyle.State_MouseOver)
        p.setPen(theme.qc(t.accent) if selected else Qt.NoPen)
        p.setBrush(theme.qc(t.pill) if selected else theme.qc(t.hover) if hovered else Qt.NoBrush)
        p.drawRoundedRect(r, theme.RADIUS["control"], theme.RADIUS["control"])

        f = opt.font
        x = r.left() + 12
        right = r.right() - 12

        badge_w = 0.0
        if cand.badge:
            f.setPixelSize(theme.FONT_SIZES["xs"])
            f.setWeight(f.Weight.DemiBold)
            p.setFont(f)
            badge_w = p.fontMetrics().horizontalAdvance(cand.badge) + 24
            pill = QRectF(right - badge_w, r.center().y() - 11, badge_w, 22)
            paint_source_pill(p, pill, cand.badge_tag, cand.badge, f, show_dot=False)
            right = pill.left() - 10

        dur_w = 0.0
        if cand.duration_ms:
            f.setWeight(f.Weight.Normal)
            f.setPixelSize(theme.FONT_SIZES["xs"])
            p.setFont(f)
            dur = fmt_ms(cand.duration_ms)
            dur_w = p.fontMetrics().horizontalAdvance(dur)
            p.setPen(theme.qc(t.text_muted))
            p.drawText(QRectF(right - dur_w, r.top(), dur_w, r.height()), Qt.AlignVCenter | Qt.AlignRight, dur)
            right -= dur_w + 12

        text_w = max(40.0, right - x)
        f.setPixelSize(theme.FONT_SIZES["md"])
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        p.setPen(theme.qc(t.text))
        p.drawText(QRectF(x, r.top() + 10, text_w, 22), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(cand.title, Qt.ElideRight, int(text_w)))

        f.setPixelSize(theme.FONT_SIZES["sm"])
        f.setWeight(f.Weight.Normal)
        p.setFont(f)
        p.setPen(theme.qc(t.text_muted))
        p.drawText(QRectF(x, r.top() + 32, text_w, 20), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(cand.subtitle, Qt.ElideRight, int(text_w)))
        p.restore()


class CandidateDialog(QDialog):
    """Modal picker shown when search/matching confidence is insufficient for auto-selection."""

    def __init__(self, title: str, subtitle: str, items: list[CandidateItem], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(540, 400)
        self.chosen: object = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[5], theme.SPACE[5], theme.SPACE[5], theme.SPACE[5])
        lay.setSpacing(theme.SPACE[3])

        head = QLabel(title)
        head.setObjectName("H2")
        sub = QLabel(subtitle)
        sub.setObjectName("Body")
        sub.setWordWrap(True)
        lay.addWidget(head)
        lay.addWidget(sub)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(theme.SPACE[2], theme.SPACE[2], theme.SPACE[2], theme.SPACE[2])
        self.list = QListWidget()
        self.list.setItemDelegate(_CandidateDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for cand in items:
            it = QListWidgetItem()
            it.setData(CAND_ROLE, cand)
            it.setSizeHint(QSize(0, ROW_H))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _: self._confirm())
        cl.addWidget(self.list)
        lay.addWidget(card, 1)

        row = QHBoxLayout()
        row.addStretch()
        self.btn_cancel = make_button(lang.t("dialogs.cancel"), "Ghost", self.reject)
        self.btn_select = make_button(lang.t("dialogs.select"), "Primary", self._confirm)
        self.btn_select.setEnabled(self.list.count() > 0)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_select)
        lay.addLayout(row)

    def _confirm(self):
        it = self.list.currentItem()
        if it is not None:
            cand: CandidateItem = it.data(CAND_ROLE)
            self.chosen = cand.payload if cand else None
            self.accept()

    @classmethod
    def pick(cls, title: str, subtitle: str, items: list[CandidateItem], parent=None):
        dlg = cls(title, subtitle, items, parent=parent)
        return dlg.chosen if dlg.exec() == QDialog.Accepted else None
