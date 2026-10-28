import ctypes
import re
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from marea.ui import theme
from marea.ui.window import MainWindow
from tests.test_spotify_ui import until

UI = Path(__file__).parent.parent / "marea" / "ui"


def settle(app, ms=350):
    QTest.qWait(ms)  # deja terminar la animación del sidebar (200 ms)


def test_no_loose_colors_in_widgets():
    """Todo el estilo vive en theme.py: ningún otro archivo de ui/ define colores."""
    pat = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")
    for f in UI.glob("*.py"):
        if f.name != "theme.py":
            assert not pat.search(f.read_text(encoding="utf-8")), f.name


def test_qss_uses_tokens_and_scale():
    qss = theme.build_qss(theme.DARK)
    assert theme.DARK.bg == "#0E0D1A" and "border-radius: 12px" in qss and "1px solid rgba(255,255,255,0.08)" in qss
    assert {theme.FONT_SIZES[k] for k in theme.FONT_SIZES} == {12, 13, 15, 20, 28}
    assert theme.qc("rgba(255,255,255,0.08)").alpha() == 20 and theme.qc("#2B56FF").red() == 0x2B


def test_sidebar_toggle_widths_tooltips_and_shortcut(qapp, make_window):
    win = make_window()
    win.resize(1400, 800)
    win.show()
    assert win.sidebar.width() == 220 and not win.sidebar.buttons[0].toolTip()
    shortcut = next(s for s in win.findChildren(QShortcut) if s.key() == QKeySequence("Ctrl+B"))
    assert shortcut.context() == Qt.ApplicationShortcut
    shortcut.activated.emit()  # offscreen no tiene ventana activa: se verifica el cableado, no el teclado
    settle(qapp)
    assert until(lambda: win.sidebar.width() == 64)  # espera a que termine la animación (con la máquina cargada tarda más)
    assert win.sidebar.collapsed and win.sidebar.buttons[2].toolTip() == "Biblioteca"
    win.sidebar.toggle.click()
    settle(qapp)
    assert until(lambda: win.sidebar.width() == 220)
    assert not win.sidebar.collapsed and not win.sidebar.buttons[2].toolTip()


def test_auto_collapse_below_1200_keeps_user_preference(qapp, make_window):
    win = make_window()
    win.resize(1400, 800)
    win.show()
    win.resize(1150, 800)
    settle(qapp)
    assert win.sidebar.collapsed
    win.resize(1400, 800)
    settle(qapp)
    assert not win.sidebar.collapsed  # volvió a lo que el usuario tenía
    win.sidebar.set_collapsed(True)  # el usuario lo cierra a propósito
    win.resize(1150, 800)
    win.resize(1400, 800)
    settle(qapp)
    assert win.sidebar.collapsed


def test_navigation_and_empty_states(qapp, make_window):
    win = make_window()
    win.show()
    assert win.pages.count() == 6
    for i in range(6):
        win.sidebar.buttons[i].click()
        settle(qapp, 250)
        assert win.pages.currentIndex() == i and win.sidebar.current() == i


def test_settings_persist_between_sessions(qapp, make_window, tmp_path):
    a = make_window("p.ini")
    a.show()
    a.sidebar.set_collapsed(True, animate=False)
    a.set_theme("light")
    a.close()
    raw = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)  # ya está en disco, sin esperar a destruir `a`
    assert raw.value("geometry") is not None and raw.value("theme") == "light"
    b = make_window("p.ini")
    b.show()
    assert b.sidebar.collapsed and theme.saved_name(b.settings) == "light"
    # (el tamaño no se compara: la plataforma offscreen simula una pantalla de 800x600 y Qt recorta la geometría)


def test_minimum_size_and_song_title(qapp, make_window):
    win = make_window()
    win.resize(500, 300)
    assert (win.width(), win.height()) == (1100, 700)
    win.set_song("Una canción con un título larguísimo " * 6)
    win.show()
    qapp.processEvents()
    assert win.titlebar.title.text().endswith("…")
    win.set_song(None)
    assert win.titlebar.title.text() == "ZH3 Marea"


def test_toast_buttons_receive_real_clicks(qapp, make_window):
    win = make_window()
    win.show()
    assert not win.toasts.isVisible()  # sin avisos la capa no tapa ni intercepta nada
    hits = []
    toast = win.toast("Listo", "ok", ("Abrir", lambda: hits.append(1)))
    qapp.processEvents()
    btn = next(b for b in toast.findChildren(QPushButton) if b.text() == "Abrir")
    pos = btn.mapTo(win, btn.rect().center())
    assert win.childAt(pos) is btn  # con WA_TransparentForMouseEvents en el host, aquí saldría la UI de abajo
    # por el QWindow, que sí hace hit-testing; QTest.mouseClick(widget) entrega el evento directo al widget y no lo probaría
    QTest.mouseClick(win.windowHandle(), Qt.LeftButton, Qt.NoModifier, pos)
    assert hits == [1] and win.toasts.count() == 0 and not win.toasts.isVisible()


def test_toast_close_button_dismisses(qapp, make_window):
    win = make_window()
    win.show()
    toast = win.toast("Algo")
    qapp.processEvents()
    close = toast.findChildren(QPushButton)[-1]
    QTest.mouseClick(win.windowHandle(), Qt.LeftButton, Qt.NoModifier, close.mapTo(win, close.rect().center()))
    assert win.toasts.count() == 0


def test_app_icon_has_taskbar_sizes_and_is_not_blank(qapp, make_window):
    ic = theme.app_icon()
    assert {16, 32, 256} <= {s.width() for s in ic.availableSizes()}
    for size in (16, 32, 256):  # chico y grande salen de SVG distintos: ambos deben tener píxeles visibles
        img = ic.pixmap(size, size).toImage()
        assert any(img.pixelColor(x, y).alpha() > 200 for x in range(0, size, 2) for y in range(0, size, 2)), size
    assert not make_window().windowIcon().isNull()


@pytest.mark.skipif(sys.platform != "win32", reason="AppUserModelID es de Windows")
def test_app_user_model_id_is_set():
    from marea import app
    app.set_app_id()
    out = ctypes.c_wchar_p()
    assert ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(out)) == 0
    assert out.value == app.APP_ID == "ZH3.Marea"


def test_reduced_motion_disables_animation(qapp, make_window, monkeypatch):
    monkeypatch.setattr(theme, "reduced_motion", lambda: True)
    win = make_window()
    win.show()
    win.sidebar.set_collapsed(True)
    assert win.sidebar.width() == 64  # sin animar: inmediato
    assert theme.duration() == 0
