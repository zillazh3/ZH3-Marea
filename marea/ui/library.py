"""Biblioteca: proyectos guardados (.marea.json) y clips exportados, con filtro Todo / Proyectos / Clips."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QLabel, QListView, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from marea import library
from marea.export import history
from marea.paths import reveal_in_folder
from marea.ui import lang, theme
from marea.ui.widgets import EmptyState, placeholder_pixmap, rounded_pixmap

TILE = 132
FILTERS = [("all", "library.filterAll"), ("project", "library.filterProjects"), ("clip", "library.filterClips")]
KINDS = {"syllable": "library.kindSyllable", "line": "library.kindLine"}  # tipo de letra -> clave i18n


def tile_icon(thumb: str) -> QIcon:
    px = QPixmap()
    if thumb and px.load(thumb):
        return QIcon(rounded_pixmap(px, TILE, theme.RADIUS["control"]))
    return QIcon(placeholder_pixmap(TILE, theme.RADIUS["control"], 40))


def project_caption(e: dict) -> str:
    n = e.get("verses", 0)
    if not n:
        return e.get("artist", "")
    kind = KINDS.get(e.get("kind"))
    return f"{lang.t('library.verses', count=n)} · {lang.t(kind)}" if kind else lang.t("library.verses", count=n)


def make_item(kind: str, e: dict, sub: str) -> QListWidgetItem:
    """Card for a library entry; UserRole = (kind, path) with kind 'project' or 'clip'."""
    it = QListWidgetItem(tile_icon(e.get("thumb", "")), f"{e.get('title') or lang.t('library.untitled')}\n{sub}".rstrip())
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
        h1 = QLabel()
        lang.bind(h1, "library.title")
        h1.setObjectName("H1")
        head.addWidget(h1)
        head.addStretch()
        group = QButtonGroup(self)
        self.chips = {}
        for key, text_key in FILTERS:
            b = QPushButton()
            lang.bind(b, text_key)
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
        self.empty_all = EmptyState("library", "", "", [("", on_missing, False)] if on_missing else ())
        lang.bind(self.empty_all.head, "library.emptyTitle")
        lang.bind(self.empty_all.body, "library.emptyBody")
        if on_missing:
            lang.bind(self.empty_all.buttons[0], "library.goHome")
        self.empty_filtered = EmptyState("library", "", "")
        lang.bind(self.empty_filtered.head, "library.filteredTitle")
        lang.bind(self.empty_filtered.body, "library.filteredBody")
        self.stack = QStackedWidget()
        for w in (self.list, self.empty_all, self.empty_filtered):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        theme.signals.changed.connect(self.reload)
        lang.signals.changed.connect(self.reload)  # los subtítulos de las tarjetas llevan texto traducido
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
        m.addAction(lang.t("library.showInFolder"), lambda: reveal_in_folder(path))
        if kind == "project":
            m.addAction(lang.t("library.remove"), lambda: (library.remove(path), self.reload()))
        m.exec(self.list.viewport().mapToGlobal(pos))
