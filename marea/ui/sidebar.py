"""Sidebar desplegable: 220 px con etiquetas, 64 px solo íconos; el ancho se anima y recorta las etiquetas."""
from PySide6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from marea.ui import theme
from marea.ui.widgets import ElidedLabel

ITEMS = [("house", "Inicio"), ("search", "Buscar"), ("library", "Biblioteca"), ("sliders-horizontal", "Editor"),
         ("download", "Exportaciones"), ("settings", "Ajustes")]
ICON = 20
LABEL_X = 64  # las etiquetas empiezan donde acaba la columna de íconos colapsada


class NavButton(QAbstractButton):
    def __init__(self, icon_name: str, label: str):
        super().__init__()
        self.icon_name, self.label, self._hover = icon_name, label, 0.0
        self.vis_w = theme.SIDEBAR["expanded"]  # ancho visible del sidebar (el botón siempre mide 220)
        self.setCheckable(True)
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._anim = QVariantAnimation(self, valueChanged=self._set_hover)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        theme.signals.changed.connect(self.update)

    def _set_hover(self, v):
        self._hover = v
        self.update()

    def _fade(self, to: float):
        self._anim.stop()
        self._anim.setDuration(theme.duration("fast"))
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(to)
        self._anim.start()

    def enterEvent(self, e):
        self._fade(1.0)

    def leaveEvent(self, e):
        self._fade(0.0)

    def paintEvent(self, e):
        t, p = theme.current, QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        pill = QRectF(8, 2, self.vis_w - 16, h - 4)
        if self.isChecked():  # sección activa: píldora sutil + ícono en acento, sin barras gruesas
            p.setBrush(theme.qc(t.pill))
        else:
            c = theme.qc(t.hover)
            c.setAlphaF(c.alphaF() * self._hover)
            p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(pill, 10, 10)
        if self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(theme.qc(t.accent))
            p.drawRoundedRect(pill.adjusted(1, 1, -1, -1), 10, 10)
        color = t.accent_icon if self.isChecked() else t.text if self._hover > 0.5 else t.text_muted
        p.drawPixmap((LABEL_X - ICON) // 2, (h - ICON) // 2, theme.icon_pixmap(self.icon_name, color, ICON, self.devicePixelRatioF()))
        f = self.font()
        f.setWeight(f.Weight.DemiBold if self.isChecked() else f.Weight.Medium)
        f.setPixelSize(theme.FONT_SIZES["sm"] + 1)
        p.setFont(f)
        p.setPen(theme.qc(t.text if self.isChecked() or self._hover > 0.5 else t.text_muted))
        p.drawText(QRectF(LABEL_X, 0, 160, h), Qt.AlignVCenter | Qt.AlignLeft, self.label)


class StatusDot(QWidget):
    def __init__(self):
        super().__init__()
        self.ok = False
        self.setFixedSize(8, 8)
        theme.signals.changed.connect(self.update)

    def set_ok(self, ok: bool):
        self.ok = ok
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc(theme.current.ok if self.ok else theme.current.text_faint))
        p.drawEllipse(self.rect())


class StatusRow(QWidget):
    def __init__(self, name: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(LABEL_X // 2 - 4, 2, 0, 2)  # el punto queda centrado en la columna de íconos
        lay.setSpacing(LABEL_X - LABEL_X // 2 - 4)
        self.dot = StatusDot()
        self.label = QLabel(name)
        self.label.setObjectName("Muted")
        lay.addWidget(self.dot)
        lay.addWidget(self.label)
        lay.addStretch()

    def set_connected(self, ok: bool):
        self.dot.set_ok(ok)


class MiniPlayer(QWidget):
    """Qué suena en Spotify, con anterior / play-pausa / siguiente (los comandos los ejecuta quien escuche las señales)."""
    prev_clicked, play_clicked, next_clicked = Signal(), Signal(), Signal()

    def __init__(self):
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 4, 12, 4)
        col.setSpacing(theme.SPACE[1])
        top = QHBoxLayout()
        top.setSpacing(theme.SPACE[3])
        self.cover = QFrame()
        self.cover.setObjectName("Cover")
        self.cover.setFixedSize(40, 40)
        self.cover_icon = QLabel(self.cover)
        self.cover_icon.setGeometry(10, 10, 20, 20)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title, self.artist = ElidedLabel(), ElidedLabel()
        self.title.setObjectName("PlayerTitle")
        self.artist.setObjectName("PlayerArtist")
        for lb in (self.title, self.artist):
            text.addWidget(lb)
        top.addWidget(self.cover)
        top.addLayout(text)
        col.addLayout(top)
        self.controls = QWidget()  # se oculta con el sidebar colapsado: ahí solo cabe la portada
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE[2])
        self.prev, self.play, self.next = (self._button(tip, sig) for tip, sig in (
            ("Anterior", self.prev_clicked), ("Reproducir / pausar", self.play_clicked), ("Siguiente", self.next_clicked)))
        row.addStretch()
        for b in (self.prev, self.play, self.next):
            row.addWidget(b)
        row.addStretch()
        col.addWidget(self.controls)
        self.playing = False
        theme.signals.changed.connect(self._refresh)
        self.set_track(None, None)

    @staticmethod
    def _button(tooltip: str, signal) -> QPushButton:
        b = QPushButton()
        b.setObjectName("IconBtn")
        b.setFixedSize(32, 32)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tooltip)
        b.clicked.connect(signal)
        return b

    def set_track(self, title, artist, playing=False):
        self.has_track, self.playing = bool(title), playing
        self.title.set_full_text(title or "Nada sonando")
        self.artist.set_full_text(artist or "Conecta Spotify")
        for b in (self.prev, self.play, self.next):
            b.setEnabled(self.has_track)
        self._refresh()

    def _refresh(self):
        t, dpr = theme.current, self.devicePixelRatioF()
        self.cover_icon.setPixmap(theme.icon_pixmap("disc-3", t.text_faint, ICON, dpr))
        color = t.text if self.has_track else t.text_faint
        self.prev.setIcon(theme.icon("skip-back", color, 16))
        self.play.setIcon(theme.icon("pause" if self.playing else "play", color, 16))
        self.next.setIcon(theme.icon("skip-forward", color, 16))


class Sidebar(QFrame):
    navigated = Signal(int)
    collapsed_changed = Signal(bool)

    def __init__(self, logo_pixmap_fn=theme.logo_pixmap):
        super().__init__()
        self.setObjectName("Sidebar")
        self._collapsed = False
        self._anim = QVariantAnimation(self, valueChanged=self._set_width)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        # el contenido mide siempre 220 px y el sidebar lo recorta al encogerse (si no, el layout lo aprieta)
        self.inner = QWidget(self)
        lay = QVBoxLayout(self.inner)
        lay.setContentsMargins(0, theme.SPACE[3], 0, theme.SPACE[3])
        lay.setSpacing(2)

        head = QHBoxLayout()  # hamburguesa siempre visible; logo y nombre se revelan al expandir
        head.setContentsMargins(12, 0, 0, 0)
        head.setSpacing(theme.SPACE[3])  # el logo empieza justo donde acaba la columna colapsada (64 px)
        self.toggle = QPushButton()
        self.toggle.setObjectName("IconBtn")
        self.toggle.setFixedSize(40, 40)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setToolTip("Alternar menú (Ctrl+B)")
        self.toggle.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        self.logo = QLabel()
        self.logo.setFixedSize(26, 26)
        self.logo.setPixmap(logo_pixmap_fn(26, self.devicePixelRatioF()))
        self.name = QLabel("ZH3 Marea")
        self.name.setObjectName("SidebarName")
        self.name.setFixedWidth(100)
        for w in (self.toggle, self.logo, self.name):
            head.addWidget(w)
        head.addStretch()
        lay.addLayout(head)
        lay.addSpacing(theme.SPACE[3])

        self.group, self.buttons = QButtonGroup(self), []
        for i, (icon_name, label) in enumerate(ITEMS):
            b = NavButton(icon_name, label)
            b.clicked.connect(lambda _=False, i=i: self.navigated.emit(i))
            self.group.addButton(b, i)
            self.buttons.append(b)
            lay.addWidget(b)
        self.buttons[0].setChecked(True)

        lay.addStretch()
        self.spotify = StatusRow("Spotify")
        lay.addWidget(self.spotify)
        lay.addSpacing(theme.SPACE[2])
        self.player = MiniPlayer()
        lay.addWidget(self.player)
        theme.signals.changed.connect(self._refresh)
        self._refresh()
        self.set_collapsed(False, animate=False)

    def _refresh(self):
        self.toggle.setIcon(theme.icon("menu", theme.current.text_muted, 20))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.inner.setGeometry(0, 0, theme.SIDEBAR["expanded"], self.height())

    def _set_width(self, w):
        w = round(w)
        self.setFixedWidth(w)
        for b in self.buttons:
            b.vis_w = w
            b.update()

    def current(self) -> int:
        return self.group.checkedId()

    def select(self, index: int):
        self.buttons[index].setChecked(True)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool, animate: bool = True):
        self._collapsed = collapsed
        target = theme.SIDEBAR["collapsed" if collapsed else "expanded"]
        for b in self.buttons:  # tooltip solo cuando las etiquetas no se ven
            b.setToolTip(b.label if collapsed else "")
        self.player.controls.setVisible(not collapsed)
        self._anim.stop()
        if animate and theme.duration() and self.isVisible():
            self._anim.setDuration(theme.duration())
            self._anim.setStartValue(self.width())
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._set_width(target)
        self.collapsed_changed.emit(collapsed)
