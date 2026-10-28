"""Exportaciones: la cola de trabajos con miniatura, progreso, tiempo estimado y acciones."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from marea.paths import reveal_in_folder
from marea.export.queue import ACTIVE, AUDIO, DONE, QUEUED, RENDER, ExportQueue, QueueItem
from marea.ui import theme
from marea.ui.screens import EmptyState
from marea.ui.widgets import ElidedLabel, placeholder_pixmap, rounded_pixmap

THUMB = 56


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return f"quedan {int(seconds // 60)}:{int(seconds % 60):02d}"


class ExportRow(QFrame):
    def __init__(self, item: QueueItem, queue: ExportQueue):
        super().__init__()
        self.item, self.queue = item, queue
        self.setObjectName("Card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[3], theme.SPACE[3], theme.SPACE[4], theme.SPACE[3])
        lay.setSpacing(theme.SPACE[4])
        self.thumb = QLabel()
        self.thumb.setFixedSize(THUMB, THUMB)
        mid = QVBoxLayout()
        mid.setSpacing(theme.SPACE[1])
        self.title = ElidedLabel(f"{item.artist} – {item.title}" if item.artist else item.title)
        self.title.setObjectName("PlayerTitle")
        self.sub = ElidedLabel()
        self.sub.setObjectName("Muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        for w in (self.title, self.sub, self.bar):
            mid.addWidget(w)
        self.eta = QLabel()
        self.eta.setObjectName("Caption")
        self.eta.setMinimumWidth(84)
        self.eta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(theme.SPACE[2])
        lay.addWidget(self.thumb)
        lay.addLayout(mid, 1)
        lay.addWidget(self.eta)
        lay.addLayout(self.buttons)
        self._set_thumb()
        self.refresh()

    def _set_thumb(self):
        px = QPixmap()
        if self.item.thumb and px.load(str(self.item.thumb)):
            self.thumb.setPixmap(rounded_pixmap(px, THUMB, theme.RADIUS["control"]))
        elif px.loadFromData(self.item.cover_png):
            self.thumb.setPixmap(rounded_pixmap(px, THUMB, theme.RADIUS["control"]))
        else:
            self.thumb.setPixmap(placeholder_pixmap(THUMB, theme.RADIUS["control"]))

    def _button(self, text: str, callback, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("Primary" if primary else "Ghost")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(callback)
        self.buttons.addWidget(b)
        return b

    def refresh(self):
        it = self.item
        while self.buttons.count():
            old = self.buttons.takeAt(0).widget()
            old.hide()
            old.setParent(None)  # fuera ya (deleteLater tarda): si no, quedaría un botón viejo junto a los nuevos
            old.deleteLater()
        self.bar.setVisible(it.state in (QUEUED, AUDIO, RENDER, DONE))
        self.bar.setValue(round(it.progress * 1000))
        self.sub.set_full_text(it.error or it.stage)
        self.eta.setText(fmt_eta(it.eta) if it.state in ACTIVE else "")
        if it.state in (QUEUED,) + ACTIVE:
            self._button("Cancelar", self._cancel)
        elif it.state == DONE:
            self._button("Abrir carpeta", lambda: reveal_in_folder(str(it.out_path)), True)
            self._button("Copiar ruta", lambda: QApplication.clipboard().setText(str(it.out_path)))
            self._button("Quitar", lambda: self.queue.remove(it))
        else:
            self._button("Quitar", lambda: self.queue.remove(it))
        if it.thumb:
            self._set_thumb()

    def update_progress(self):
        self.bar.setValue(round(self.item.progress * 1000))
        self.sub.set_full_text(self.item.stage)
        self.eta.setText(fmt_eta(self.item.eta))

    def _cancel(self):
        if self.item.state in ACTIVE:  # único diálogo modal permitido: confirmar que se descarta un trabajo en curso
            box = QMessageBox(QMessageBox.Question, "Cancelar exportación",
                              "¿Cancelar esta exportación? Se descartará el video a medias.", QMessageBox.Yes | QMessageBox.No, self)
            box.button(QMessageBox.Yes).setText("Sí, cancelar")
            box.button(QMessageBox.No).setText("Seguir exportando")
            if box.exec() != QMessageBox.Yes:
                return
        self.queue.cancel(self.item)


class ExportsPage(QWidget):
    def __init__(self, queue: ExportQueue):
        super().__init__()
        self.setObjectName("Page")
        self.queue, self.rows = queue, {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])
        head = QHBoxLayout()
        h1 = QLabel("Exportaciones")
        h1.setObjectName("H1")
        self.btn_clear = QPushButton("Limpiar terminadas")
        self.btn_clear.setObjectName("Ghost")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(queue.clear_finished)
        head.addWidget(h1)
        head.addStretch()
        head.addWidget(self.btn_clear)
        lay.addLayout(head)
        self.stack = QStackedWidget()
        self.empty = EmptyState("download", "Todavía no hay exportaciones",
                                "Los clips que exportes desde el Editor aparecerán aquí con su progreso.")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.holder = QWidget()
        self.col = QVBoxLayout(self.holder)
        self.col.setContentsMargins(0, 0, theme.SPACE[2], 0)
        self.col.setSpacing(theme.SPACE[3])
        self.col.addStretch()
        self.scroll.setWidget(self.holder)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.scroll)
        lay.addWidget(self.stack, 1)
        queue.changed.connect(self.rebuild)
        queue.item_changed.connect(lambda it: self.rows[it].update_progress() if it in self.rows else None)
        self.rebuild()

    def rebuild(self):
        for it in [i for i in self.rows if i not in self.queue.items]:
            self.rows.pop(it).deleteLater()
        for pos, it in enumerate(self.queue.items):
            row = self.rows.get(it)
            if row is None:
                row = self.rows[it] = ExportRow(it, self.queue)
                self.col.insertWidget(pos, row)
            else:
                row.refresh()
        self.stack.setCurrentWidget(self.scroll if self.queue.items else self.empty)
        self.btn_clear.setEnabled(any(i.finished for i in self.queue.items))
