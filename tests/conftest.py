import os
import sys
import traceback

import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # los tests de UI no abren ventanas en el escritorio
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="marea_test_")  # los tests no tocan el historial ni la caché reales

import pytest


@pytest.fixture(autouse=True)
def fail_on_unhandled_qt_exceptions():
    """PySide imprime (no propaga) las excepciones de paintEvent, slots y señales: sin esto, un dibujo roto pasa en verde."""
    errors, previous = [], sys.excepthook
    sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))
    yield
    sys.excepthook = previous
    assert not errors, "Excepción no capturada dentro de Qt:\n" + "\n".join(errors)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    from marea.ui import theme

    from PySide6.QtCore import QThreadPool

    app = QApplication.instance() or QApplication([])
    theme.load_fonts()
    theme.apply(app, "dark")
    yield app
    QThreadPool.globalInstance().waitForDone(5000)  # que ningún hilo emita sobre objetos ya destruidos al cerrar
    app.processEvents()


@pytest.fixture
def make_window(qapp, tmp_path):
    """Ventanas con ajustes temporales (INI en tmp_path), sin Mica; se cierran solas."""
    from PySide6.QtCore import QSettings

    from marea.ui import theme
    from marea.ui.window import MainWindow

    made = []

    def make(ini="t.ini", **kw):
        win = MainWindow(settings=QSettings(str(tmp_path / ini), QSettings.IniFormat), mica=False, **kw)
        made.append(win)
        return win

    yield make
    for w in made:
        w.close()
    theme.apply(qapp, "dark")
