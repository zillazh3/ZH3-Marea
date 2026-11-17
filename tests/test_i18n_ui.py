"""Live language switching on the real window, and a leak check: no Spanish text left on screen in English."""
import re

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QLineEdit, QTabBar, QWidget

from marea import i18n
from marea.ui import lang
from marea.ui.sidebar import NavButton

SPANISH = re.compile(r"[áéíóúñ¿¡]|\b(Inicio|Buscar|Ajustes|Biblioteca|Exportaciones|Guardar|Cancelar|Abrir|Letra|Fondo|Elige|Aplicar|Borrar)\b")
# Text that is content or brand, not UI chrome: the demo song and native language names.
ALLOWED = ("Marea de luz", "Demo · letra original", "Español", "Zilla-ZH3")


def ui_texts(root: QWidget) -> list[tuple[str, str]]:
    """(where, text) for every piece of visible UI text under `root`, menus and tab bars included."""
    out = []
    for w in [root, *root.findChildren(QWidget)]:
        name = f"{type(w).__name__}:{w.objectName()}"
        out += [(f"{name}.tooltip", w.toolTip()), (f"{name}.accessible", w.accessibleName()), (f"{name}.title", w.windowTitle())]
        if isinstance(w, (QLabel, QAbstractButton)):
            out.append((f"{name}.text", w.text()))
        if isinstance(w, QLineEdit):
            out.append((f"{name}.placeholder", w.placeholderText()))
        if isinstance(w, QComboBox):
            out += [(f"{name}.item{i}", w.itemText(i)) for i in range(w.count())]
        if isinstance(w, QTabBar):
            out += [(f"{name}.tab{i}", w.tabText(i)) for i in range(w.count())]
        for a in w.findChildren(QAction):
            out.append((f"{name}.action", a.text()))
            out.append((f"{name}.action.tooltip", a.toolTip()))
    return [(where, text) for where, text in out if text.strip()]


def leaks(root: QWidget) -> list[str]:
    bad = []
    for where, text in ui_texts(root):
        clean = text
        for ok in ALLOWED:
            clean = clean.replace(ok, "")
        if SPANISH.search(clean):
            bad.append(f"{where}: {text!r}")
    return sorted(set(bad))


@pytest.fixture
def window(make_window):
    win = make_window()
    win.load_demo()
    win.show()
    return win


def test_live_switch_changes_sidebar_settings_and_editor_texts(window, qapp):
    sidebar_tips = lambda: [b.label for b in window.sidebar.findChildren(NavButton)]  # noqa: E731
    es_tips = sidebar_tips()
    i18n.set_language("en")
    qapp.processEvents()
    en_tips = sidebar_tips()
    assert "Library" in en_tips and "Biblioteca" in es_tips
    assert window.settings_body.lang_combo.itemText(0) == "System (auto)"
    assert window.page_settings.h1.text() == "Settings"
    i18n.set_language("es")
    qapp.processEvents()
    assert sidebar_tips() == es_tips  # back to Spanish exactly, nothing rebuilt or lost
    assert window.editor.project is not None


def test_language_selector_persists_and_applies(window, qapp):
    body = window.settings_body
    body.lang_combo.setCurrentIndex(body.lang_combo.findData("en"))
    assert window.settings.value("language") == "en" and i18n.current() == "en"
    body.lang_combo.setCurrentIndex(body.lang_combo.findData("system"))
    assert window.settings.value("language") == "system"


def test_system_preference_follows_os_languages(window, monkeypatch):
    monkeypatch.setattr(lang, "system_ui_languages", lambda: ["es-MX", "en-US"])
    assert lang.apply_preference("system") == "es"
    monkeypatch.setattr(lang, "system_ui_languages", lambda: ["de-DE"])
    assert lang.apply_preference("system") == "en"  # unsupported -> English


def test_no_spanish_left_in_english(window, qapp):
    i18n.set_language("en")
    qapp.processEvents()
    found = []
    for page_index in range(window.pages.count()):
        window.sidebar.select(page_index)
        window.pages.setCurrentIndex(page_index)
        qapp.processEvents()
        found += leaks(window)
    # every editor tab, not only the visible one
    tabs = window.editor.findChild(QTabBar) if hasattr(window.editor, "findChild") else None
    if tabs is not None:
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            qapp.processEvents()
            found += leaks(window.editor)
    assert not sorted(set(found)), "Spanish text still shown in English:\n" + "\n".join(sorted(set(found)))


def test_spanish_untouched_by_the_catalog(window):
    """Spanish catalog texts must match what the app showed before i18n (spot-check the shell)."""
    assert {i18n.t(k) for k in ("nav.home", "nav.search")} == {"Inicio", "Buscar"}
