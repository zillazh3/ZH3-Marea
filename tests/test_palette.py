from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from marea.ui.palette import CommandPalette


def test_palette_filters_ignoring_accents_and_runs_the_highlighted_command(qapp):
    ran = []
    cmds = [("Ir a", "Inicio", lambda: ran.append("home")), ("Ir a", "Biblioteca", lambda: ran.append("lib")),
            ("Acción", "Abrir proyecto", lambda: ran.append("open"))]
    pal = CommandPalette(None, cmds)
    assert pal.list.count() == 3 and pal.list.currentRow() == 0
    pal.edit.setText("ACCION")  # group names are not searched, labels are
    assert pal.list.count() == 0
    pal.edit.setText("bibli")
    assert pal.list.count() == 1
    pal.edit.setText("")
    QTest.keyClick(pal.edit, Qt.Key_Down)
    QTest.keyClick(pal.edit, Qt.Key_Return)
    assert ran == ["lib"]


def test_window_palette_jumps_to_a_screen_and_toggles_the_theme(make_window, qapp):
    win = make_window()
    win.show()
    cmds = {label: cb for _, label, cb in win.palette_commands()}
    assert {"Inicio", "Buscar", "Editor", "Biblioteca", "Exportaciones", "Ajustes", "Cambiar tema"} <= set(cmds)
    cmds["Biblioteca"]()
    assert win.pages.currentIndex() == 3 and win.sidebar.current() == 3
    before = win.settings.value("theme")
    cmds["Cambiar tema"]()
    assert win.settings.value("theme") != before
    win.sidebar.command.click()  # opens the popup without raising
    win.findChild(CommandPalette).close()
