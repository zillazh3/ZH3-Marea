"""Paleta de comandos (Ctrl+K): escribe unas letras para saltar a una pantalla o lanzar una acción."""
import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from marea.ui import lang, theme

WIDTH, MAX_VISIBLE = 520, 9


def _fold(text: str) -> str:
    """Lowercase without accents, so «ajus» finds «Ajustes» and «biblio» finds «Biblioteca»."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


class CommandPalette(QDialog):
    def __init__(self, parent, commands):
        """commands: [(group, label, callback)]; Enter runs the highlighted one, Esc or a click outside closes."""
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("Palette")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.commands = commands
        self.setFixedWidth(WIDTH)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[3], theme.SPACE[3], theme.SPACE[3], theme.SPACE[3])
        lay.setSpacing(theme.SPACE[2])
        self.edit = QLineEdit()
        self.edit.setObjectName("Search")
        lang.bind(self.edit, "palette.placeholder", "setPlaceholderText")
        self.edit.textChanged.connect(self._filter)
        self.edit.installEventFilter(self)
        self.list = QListWidget()
        self.list.setObjectName("Intervals")
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.itemClicked.connect(lambda _: self._run())
        lay.addWidget(self.edit)
        lay.addWidget(self.list)
        self._filter("")

    def _filter(self, text: str):
        q = _fold(text.strip())
        self.list.clear()
        for group, label, callback in self.commands:
            if q in _fold(label):
                it = QListWidgetItem(f"{label}    ·  {group}")
                it.setData(Qt.UserRole, callback)
                self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.list.setFixedHeight(max(1, min(self.list.count(), MAX_VISIBLE)) * 30 + 8)
        self.adjustSize()

    def _run(self):
        item = self.list.currentItem()
        if item is not None:
            self.accept()  # close first: the action may open dialogs or change pages
            item.data(Qt.UserRole)()

    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == e.Type.KeyPress:
            key = e.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                step = 1 if key == Qt.Key_Down else -1
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + step)))
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._run()
                return True
        return super().eventFilter(obj, e)

    def popup(self):
        """Centered near the top of the parent window, with the search box focused."""
        p = self.parentWidget()
        self.move(p.x() + (p.width() - WIDTH) // 2, p.y() + p.height() // 6)
        self.edit.clear()
        self.show()
        self.edit.setFocus()
