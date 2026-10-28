"""Avisos discretos abajo a la derecha (sin diálogos modales)."""
from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from marea.ui import theme

MARGIN = theme.SPACE[5]


class Toast(QFrame):
    def __init__(self, text: str, kind: str = "info", action=None, ms: int = 5000):
        super().__init__()
        self.setObjectName("Toast")
        self.setProperty("kind", kind)
        self.setMaximumWidth(420)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[4], theme.SPACE[3], theme.SPACE[3], theme.SPACE[3])
        lay.setSpacing(theme.SPACE[3])
        label = QLabel(text)
        label.setWordWrap(True)
        lay.addWidget(label, 1)
        if action:
            btn = QPushButton(action[0])
            btn.setObjectName("Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda: (action[1](), self.dismiss()))
            lay.addWidget(btn)
        close = QPushButton()
        close.setObjectName("IconBtn")
        close.setFixedSize(28, 28)
        close.setIcon(theme.icon("x", theme.current.text_muted, 14))
        close.clicked.connect(self.dismiss)
        lay.addWidget(close)
        timer = QTimer(self)  # hijo del toast: muere con él y no dispara sobre un widget ya destruido
        timer.setSingleShot(True)
        timer.timeout.connect(self.dismiss)
        timer.start(ms)

    def dismiss(self):
        if self.parent() is None:
            return
        self.hide()
        self.setParent(None)
        self.deleteLater()


class ToastHost(QWidget):
    """Pila de avisos en la esquina inferior derecha. Mide solo lo que ocupan los avisos (y se oculta sin ninguno):
    un widget transparente a los clics no sirve, Qt tampoco se los entrega a sus hijos."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(theme.SPACE[2])
        self.hide()

    def event(self, e):
        handled = super().event(e)
        if e.type() == QEvent.LayoutRequest:  # entró o salió un aviso
            self._fit()
        return handled

    def _fit(self):
        p = self.parentWidget()
        if p is None:
            return
        n = self._lay.count()
        self.setVisible(n > 0)
        if not n:
            return
        w = min(self._lay.sizeHint().width(), p.width() - 2 * MARGIN)
        h = min(self._lay.totalHeightForWidth(w), p.height() - 2 * MARGIN)
        self.setGeometry(p.width() - MARGIN - w, p.height() - MARGIN - h, w, h)
        self.raise_()

    def show_toast(self, text: str, kind: str = "info", action=None) -> Toast:
        t = Toast(text, kind, action, ms=8000 if kind == "error" else 5000)
        self._lay.addWidget(t, alignment=Qt.AlignRight)
        self._fit()
        fx = QGraphicsOpacityEffect(t)
        t.setGraphicsEffect(fx)
        anim = QPropertyAnimation(fx, b"opacity", t)
        anim.setDuration(theme.duration("fast"))
        anim.setStartValue(0.0 if theme.duration("fast") else 1.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda: t.setGraphicsEffect(None))
        anim.start()
        return t

    def count(self) -> int:
        return self._lay.count()

    def resize_to(self, parent_size=None):
        self._fit()
