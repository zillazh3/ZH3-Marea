from contextlib import contextmanager

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from marea.ui import theme

# tipo de letra disponible (el valor viene de Lyrics.kind o 'none'/'error') -> clave i18n; resolver con lang.t al usarla
LYRICS_BADGES = {"syllable": "widgets.badgeSyllable", "line": "widgets.badgeLine", "none": "widgets.badgeNone",
                 "error": "widgets.badgeError"}


def fmt_ms(ms: float, tenths: bool = False, millis: bool = False) -> str:
    """Format milliseconds as M:SS, M:SS.t, or M:SS.mmm."""
    s = max(0.0, ms) / 1000
    if millis:
        whole_ms = round(max(0.0, ms))
        secs, rem = divmod(whole_ms, 1000)
        mins, sec = divmod(secs, 60)
        return f"{mins}:{sec:02d}.{rem:03d}"
    if tenths:
        return f"{int(s // 60)}:{s % 60:04.1f}"
    total = int(s)
    return f"{total // 60}:{total % 60:02d}"


class ClickSeekSlider(QSlider):
    """Horizontal slider where clicking anywhere along the groove jumps directly to that value."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.orientation() == Qt.Horizontal and self.width() > 0:
            ratio = max(0.0, min(1.0, e.position().x() / self.width()))
            lo, hi = self.minimum(), self.maximum()
            val = round(lo + ratio * (hi - lo))
            self.setSliderDown(True)
            self.sliderPressed.emit()
            self.setValue(val)
            self.sliderMoved.emit(val)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.isSliderDown() and self.orientation() == Qt.Horizontal and self.width() > 0:
            ratio = max(0.0, min(1.0, e.position().x() / self.width()))
            lo, hi = self.minimum(), self.maximum()
            val = round(lo + ratio * (hi - lo))
            if val != self.value():
                self.setValue(val)
                self.sliderMoved.emit(val)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            self.sliderReleased.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)


@contextmanager
def blocked_signals(widget: QObject):
    """Temporarily block Qt signals on a QObject, restoring the previous state."""
    prev = widget.blockSignals(True)
    try:
        yield widget
    finally:
        widget.blockSignals(prev)


def make_button(text: str = "", object_name: str = "Ghost", clicked=None, tooltip: str = "") -> QPushButton:
    """Create a consistently styled QPushButton with pointing-hand cursor."""
    btn = QPushButton(text)
    if object_name:
        btn.setObjectName(object_name)
    btn.setCursor(Qt.PointingHandCursor)
    if tooltip:
        btn.setToolTip(tooltip)
    if clicked is not None:
        btn.clicked.connect(clicked)
    return btn


def fade_in(target: QWidget, duration_ms: int | None = None, on_done=None,
            owner: QObject | None = None) -> QPropertyAnimation | None:
    """Fade a widget from transparent to opaque when animations are enabled."""
    ms = theme.duration() if duration_ms is None else duration_ms
    if not ms:
        target.setGraphicsEffect(None)
        if on_done is not None:
            on_done()
        return None
    fx = QGraphicsOpacityEffect(target)
    target.setGraphicsEffect(fx)
    anim = QPropertyAnimation(fx, b"opacity", owner or target)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def _finish():
        target.setGraphicsEffect(None)
        if on_done is not None:
            on_done()

    anim.finished.connect(_finish)
    anim.start()
    return anim


class CoalescedAction(QObject):
    """Coalesce rapid triggers into one single-shot callback after delay_ms."""

    def __init__(self, delay_ms: int, callback, parent: QObject | None = None):
        super().__init__(parent)
        self._cb = callback
        self._timer = QTimer(self, singleShot=True, interval=delay_ms)
        self._timer.timeout.connect(self._cb)

    def schedule(self):
        self._timer.start()

    def cancel(self):
        self._timer.stop()

    def flush(self):
        self._timer.stop()
        self._cb()

    def is_active(self) -> bool:
        return self._timer.isActive()


class SliderRow(QWidget):
    """Horizontal slider paired with a formatted caption readout."""
    valueChanged = Signal(int)
    sliderReleased = Signal()

    def __init__(self, lo: int, hi: int, val: int, format_fn, tooltip: str = "",
                 min_label_width: int = 40, parent: QWidget | None = None):
        super().__init__(parent)
        self._format_fn = format_fn
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(lo, hi)
        self.slider.setValue(val)
        if tooltip:
            self.slider.setToolTip(tooltip)
        self.label = QLabel(format_fn(val), self)
        self.label.setObjectName("Caption")
        self.label.setMinimumWidth(min_label_width)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.slider, 1)
        row.addWidget(self.label)
        self.slider.valueChanged.connect(self._on_changed)
        self.slider.sliderReleased.connect(self.sliderReleased)

    def _on_changed(self, value: int):
        self.label.setText(self._format_fn(value))
        self.valueChanged.emit(value)

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, value: int):
        self.slider.setValue(value)

    def update_readout(self):
        self.label.setText(self._format_fn(self.slider.value()))


def placeholder_pixmap(size: int, radius: int, icon_px: int = 24, dpr: float = 2.0) -> QPixmap:
    """Recuadro de portada vacía con un disco centrado, a tamaño nativo (sin reescalar el ícono)."""
    px = QPixmap(round(size * dpr), round(size * dpr))
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(theme.qc(theme.current.surface_hover))
    p.drawRoundedRect(QRectF(0, 0, size * dpr, size * dpr), radius * dpr, radius * dpr)
    off = round((size - icon_px) / 2 * dpr)
    p.drawPixmap(off, off, theme.icon_pixmap("disc-3", theme.current.text_faint, icon_px, dpr))
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


def rounded_pixmap(source: QPixmap, size: int, radius: int, dpr: float = 2.0) -> QPixmap:
    """Portada cuadrada recortada con esquinas redondeadas, nítida a la densidad de píxeles dada."""
    px = size * dpr
    scaled = source.scaled(round(px), round(px), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(round(px), round(px))
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, px, px), radius * dpr, radius * dpr)
    p.setClipPath(clip)
    p.drawPixmap(round((px - scaled.width()) / 2), round((px - scaled.height()) / 2), scaled)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


class ElidedLabel(QLabel):
    """QLabel de una línea que recorta con «…» según el ancho disponible."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setMinimumWidth(40)
        self.setText(text)

    def set_full_text(self, text: str):
        self._full = text
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(0, self.width())))


class EmptyState(QWidget):
    """Ícono, una frase clara y qué hacer."""

    def __init__(self, icon_name: str, title: str, message: str, actions=()):
        """actions: [(texto, callback, es_principal)] — botones bajo la frase."""
        super().__init__()
        self.icon_name = icon_name
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(theme.SPACE[3])
        self.tile = QFrame()
        self.tile.setObjectName("IconTile")
        self.tile.setFixedSize(64, 64)
        self.glyph = QLabel(self.tile)
        self.glyph.setGeometry(18, 18, 28, 28)
        head, body = QLabel(title), QLabel(message)
        self.head, self.body, self.buttons = head, body, []  # para que quien la crea los enlace a claves i18n
        head.setObjectName("H2")
        body.setObjectName("Body")
        for lb in (head, body):
            lb.setAlignment(Qt.AlignCenter)
            lb.setWordWrap(True)
        body.setMaximumWidth(440)
        lay.addWidget(self.tile, alignment=Qt.AlignCenter)
        lay.addSpacing(theme.SPACE[1])
        lay.addWidget(head)
        lay.addWidget(body, alignment=Qt.AlignCenter)
        if actions:
            row = QHBoxLayout()
            row.setAlignment(Qt.AlignCenter)
            row.setSpacing(theme.SPACE[3])
            for text, callback, primary in actions:
                self.buttons.append(make_button(text, "Primary" if primary else "Ghost", callback))
                row.addWidget(self.buttons[-1])
            lay.addSpacing(theme.SPACE[2])
            lay.addLayout(row)
        theme.signals.changed.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        self.glyph.setPixmap(theme.icon_pixmap(self.icon_name, theme.current.accent_icon, 28, self.devicePixelRatioF()))


class Page(QWidget):
    """Título de sección + una tarjeta con el contenido."""

    def __init__(self, title: str, body: QWidget, header: QWidget | None = None, framed: bool = True):
        """framed=False when the body brings its own cards (Ajustes) and a wrapping Card would nest them."""
        super().__init__()
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])
        self.h1 = h1 = QLabel(title)
        h1.setObjectName("H1")
        lay.addWidget(h1)
        if header:
            lay.addWidget(header)
        if not framed:
            lay.addWidget(body, 1)
            return
        card = QFrame()
        card.setObjectName("Card")
        QVBoxLayout(card).addWidget(body)
        card.layout().setContentsMargins(theme.SPACE[5], theme.SPACE[5], theme.SPACE[5], theme.SPACE[5])
        lay.addWidget(card, 1)


def scrollable(body: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(body)
    return area


class FadeStack(QStackedWidget):
    """Cambio de sección con fundido corto (ease-out); sin animación si Windows pide reducir movimiento."""

    def set_page(self, index: int):
        if index == self.currentIndex():
            return
        self.setCurrentIndex(index)
        page = self.currentWidget()
        if page is not None:
            self._anim = fade_in(page, owner=self)
