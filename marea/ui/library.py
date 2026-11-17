"""Biblioteca: proyectos guardados (.marea.json) y clips exportados, con filtro Todo / Proyectos / Clips."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QLabel, QListView, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from marea import library
from marea.export import history
from marea.paths import reveal_in_folder
from marea.ui import theme
from marea.ui.widgets import EmptyState, placeholder_pixmap, rounded_pixmap

TILE = 132
FILTERS = [("all", "Todo"), ("project", "Proyectos"), ("clip", "Clips")]
KINDS = {"syllable": "por sílaba", "line": "por línea"}


def tile_icon(thumb: str) -> QIcon:
    px = QPixmap()
    if thumb and px.load(thumb):
        return QIcon(rounded_pixmap(px, TILE, theme.RADIUS["control"]))
    return QIcon(placeholder_pixmap(TILE, theme.RADIUS["control"], 40))


def project_caption(e: dict) -> str:
    n = e.get("verses", 0)
    return f"{n} versos · {KINDS.get(e.get('kind'), '')}".rstrip(" ·") if n else e.get("artist", "")


def make_item(kind: str, e: dict, sub: str) -> QListWidgetItem:
    """Card for a library entry; UserRole = (kind, path) with kind 'project' or 'clip'."""
    it = QListWidgetItem(tile_icon(e.get("thumb", "")), f"{e.get('title') or 'Sin título'}\n{sub}".rstrip())
    it.setData(Qt.UserRole, (kind, e["path"]))
    it.setToolTip(e["path"])
    it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
    return it


class LibraryPage(QWidget):
    def __init__(self, on_open_project, on_missing=None):
        """on_open_project(path) abre un .marea.json; on_missing() lleva a Inicio desde el estado vacío."""
        super().__init__()
        self.setObjectName("Page")
        self.on_open_project, self.filter = on_open_project, "all"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])
        head = QHBoxLayout()
        h1 = QLabel("Biblioteca")
        h1.setObjectName("H1")
        head.addWidget(h1)
        head.addStretch()
        group = QButtonGroup(self)
        self.chips = {}
        for key, text in FILTERS:
            b = QPushButton(text)
            b.setObjectName("Chip")
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            group.addButton(b)
            head.addWidget(b)
            self.chips[key] = b
        lay.addLayout(head)

        self.list = QListWidget()
        self.list.setObjectName("Library")
        self.list.setViewMode(QListView.IconMode)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setMovement(QListView.Static)
        self.list.setWrapping(True)
        self.list.setUniformItemSizes(True)
        self.list.setIconSize(QSize(TILE, TILE))
        self.list.setGridSize(QSize(TILE + theme.SPACE[5], TILE + 64))
        self.list.setSpacing(theme.SPACE[2])
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.itemClicked.connect(self._open)
        self.list.customContextMenuRequested.connect(self._menu)
        self.empty_all = EmptyState("library", "Aún no hay nada aquí",
                                    "Guarda un proyecto desde el Editor (menú ⌄ → Guardar proyecto…) o exporta un clip, y aparecerá aquí.",
                                    [("Ir a Inicio", on_missing, False)] if on_missing else ())
        self.empty_filtered = EmptyState("library", "Nada con este filtro", "Prueba con «Todo».")
        self.stack = QStackedWidget()
        for w in (self.list, self.empty_all, self.empty_filtered):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        theme.signals.changed.connect(self.reload)
        self.reload()

    def set_filter(self, key: str):
        self.filter = key
        self.chips[key].setChecked(True)
        self.reload()

    def showEvent(self, e):
        super().showEvent(e)
        self.reload()

    def reload(self):
        self.list.clear()
        entries = []
        if self.filter in ("all", "project"):
            entries += [("project", e, project_caption(e)) for e in library.load()]
        if self.filter in ("all", "clip"):
            entries += [("clip", e, e.get("artist", "")) for e in history.load()]
        entries.sort(key=lambda t: t[1].get("date", ""), reverse=True)
        for kind, e, sub in entries:
            self.list.addItem(make_item(kind, e, sub))
        if entries:
            self.stack.setCurrentWidget(self.list)
        else:
            self.stack.setCurrentWidget(self.empty_all if self.filter == "all" else self.empty_filtered)

    def _open(self, item: QListWidgetItem):
        kind, path = item.data(Qt.UserRole)
        if kind == "project":
            self.on_open_project(path)
        else:
            reveal_in_folder(path)

    def _menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        kind, path = item.data(Qt.UserRole)
        m = QMenu(self)
        m.addAction("Mostrar en carpeta", lambda: reveal_in_folder(path))
        if kind == "project":
            m.addAction("Quitar de la biblioteca", lambda: (library.remove(path), self.reload()))
        m.exec(self.list.viewport().mapToGlobal(pos))
