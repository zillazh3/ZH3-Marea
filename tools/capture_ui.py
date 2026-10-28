"""Capturas del shell sin abrir ventanas en tu escritorio (plataforma offscreen):

    python tools/capture_ui.py   ->  out/ui_*.png
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from marea.demo_cover import demo_cover  # noqa: E402
from marea.ui import theme  # noqa: E402
from marea.ui.window import MainWindow, qimage_from_skia  # noqa: E402

OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)


def settle(app, win):
    for _ in range(5):
        app.processEvents()
    win.sidebar.update()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.apply(app, "dark")
    ini = QSettings(str(Path(tempfile.mkdtemp()) / "t.ini"), QSettings.IniFormat)  # sin tocar tus ajustes reales
    win = MainWindow(settings=ini, mica=False, cover=qimage_from_skia(demo_cover()))
    win.resize(1280, 800)
    win.set_song("Marea de luz — Demo")
    win.sidebar.player.set_track("Marea de luz", "Demo · letra original", True)
    win.sidebar.spotify.set_connected(True)
    win.show()
    settle(app, win)
    shots = [("expandido", lambda: None), ("colapsado", lambda: win.sidebar.set_collapsed(False, animate=False) or win.sidebar.set_collapsed(True, animate=False)),
             ("buscar", lambda: (win.sidebar.set_collapsed(False, animate=False), win.sidebar.select(1), win.pages.setCurrentIndex(1))),
             ("editor", lambda: (win.load_demo(), QTest.qWait(600))),  # preview en CPU (offscreen no tiene OpenGL): más borrosa
             ("claro_ajustes", lambda: (win.sidebar.select(5), win.pages.setCurrentIndex(5), theme.apply(app, "light")))]
    for name, action in shots:
        action()
        settle(app, win)
        win.grab().save(str(OUT / f"ui_{name}.png"))
        print(OUT / f"ui_{name}.png")
