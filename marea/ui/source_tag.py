"""Native ZH3 Marea lyrics source tags: shared theme-derived palette, painter helper, and interactive pill button."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QPushButton

from marea.model.lyrics import SOURCE_LRCLIB, SOURCE_MANUAL, SOURCE_SPICY, SOURCE_UNKNOWN, VALID_SOURCE_TAGS
from marea.ui import lang, theme

SOURCE_KEYS = {
    SOURCE_SPICY: "sourceTag.spicy",
    SOURCE_LRCLIB: "sourceTag.lrclib",
    SOURCE_MANUAL: "sourceTag.manual",
    SOURCE_UNKNOWN: "sourceTag.unknown",
}


def source_label(tag: str) -> str:
    norm = tag if tag in VALID_SOURCE_TAGS else SOURCE_UNKNOWN
    return lang.t(SOURCE_KEYS[norm])


def _with_alpha(color: QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def source_palette(tag: str, t: theme.Theme | None = None) -> tuple[QColor, QColor, QColor]:
    """Return `(fill, border, fg)` QColors derived strictly from ZH3 Marea theme tokens."""
    cur = t or theme.current
    norm = tag if tag in VALID_SOURCE_TAGS else SOURCE_UNKNOWN
    is_dark = cur.name == "dark"
    if norm == SOURCE_SPICY:
        fg = theme.qc(cur.accent_icon)
        fill = _with_alpha(theme.qc(cur.accent), 0.15 if is_dark else 0.10)
        border = _with_alpha(fg, 0.34 if is_dark else 0.28)
        return fill, border, fg
    if norm == SOURCE_MANUAL:
        fg = theme.qc(cur.ok)
        fill = _with_alpha(fg, 0.14 if is_dark else 0.11)
        border = _with_alpha(fg, 0.32 if is_dark else 0.28)
        return fill, border, fg
    if norm == SOURCE_LRCLIB:
        return theme.qc(cur.pill), theme.qc(cur.border), theme.qc(cur.text_muted)
    return theme.qc(cur.surface), theme.qc(cur.border), theme.qc(cur.text_faint)


def paint_source_pill(
    p: QPainter,
    rect: QRectF,
    tag: str,
    text: str,
    font: QFont,
    *,
    hover_mix: float = 0.0,
    show_dot: bool = True,
) -> None:
    """Paint a compact native pill tag inside `rect` matching ZH3 Marea typography and border tokens."""
    cur = theme.current
    fill, border, fg = source_palette(tag, cur)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    radius = min(rect.height() / 2.0, float(theme.RADIUS["pill"]))
    p.setPen(border)
    p.setBrush(fill)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    if hover_mix > 0.0:
        hov = theme.qc(cur.hover)
        hov.setAlphaF(hov.alphaF() * max(0.0, min(1.0, hover_mix)))
        p.setPen(Qt.NoPen)
        p.setBrush(hov)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    f = QFont(font)
    f.setFamily(theme.FAMILY)
    f.setPixelSize(theme.FONT_SIZES["xs"])
    f.setWeight(QFont.Weight.DemiBold)
    p.setFont(f)

    pad_l = 8.0
    if show_dot:
        dot_r = 2.75
        cx = rect.left() + pad_l + dot_r
        cy = rect.center().y()
        p.setPen(Qt.NoPen)
        p.setBrush(fg)
        p.drawEllipse(QRectF(cx - dot_r, cy - dot_r, dot_r * 2, dot_r * 2))
        text_left = cx + dot_r + 5.0
    else:
        text_left = rect.left() + pad_l

    p.setPen(fg)
    text_rect = QRectF(text_left, rect.top(), max(0.0, rect.right() - 8.0 - text_left), rect.height())
    p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
    p.restore()


class SourceTagButton(QPushButton):
    """Compact interactive lyrics-source pill button native to ZH3 Marea."""

    HEIGHT = 22

    def __init__(self, tag: str = SOURCE_UNKNOWN, parent=None):
        super().__init__(parent)
        self._tag = tag if tag in VALID_SOURCE_TAGS else SOURCE_UNKNOWN
        self._hover = 0.0
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, easingCurve=QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFlat(True)
        self.setFixedHeight(self.HEIGHT)
        theme.signals.changed.connect(self.update)
        lang.signals.changed.connect(self._sync_text)
        self._sync_text()

    @property
    def tag(self) -> str:
        return self._tag

    def set_tag(self, tag: str, tooltip: str = ""):
        self._tag = tag if tag in VALID_SOURCE_TAGS else SOURCE_UNKNOWN
        self._sync_text()
        if tooltip:
            self.setToolTip(tooltip)

    def _sync_text(self):
        label = source_label(self._tag)
        self.setText(label)
        self.setAccessibleName(label)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        f = QFont(self.font())
        f.setFamily(theme.FAMILY)
        f.setPixelSize(theme.FONT_SIZES["xs"])
        f.setWeight(QFont.Weight.DemiBold)
        self.ensurePolished()
        from PySide6.QtGui import QFontMetrics
        w = QFontMetrics(f).horizontalAdvance(source_label(self._tag)) + 26
        return QSize(max(56, w), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _on_anim(self, val):
        self._hover = float(val)
        self.update()

    def _animate(self, target: float):
        ms = theme.duration("fast")
        self._anim.stop()
        if not ms:
            self._hover = target
            self.update()
            return
        self._anim.setDuration(ms)
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, e):
        self._animate(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._animate(0.0)
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        paint_source_pill(
            p,
            QRectF(self.rect()),
            self._tag,
            source_label(self._tag),
            self.font(),
            hover_mix=self._hover,
        )
        if self.hasFocus():
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(theme.qc(theme.current.accent_icon))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
