"""Entrada de ZH3 Marea."""
import ctypes
import logging
import multiprocessing
import sys
import traceback

from PySide6.QtWidgets import QApplication

from marea.demo_cover import demo_cover
from marea.paths import app_data, make_settings
from marea.ui import theme
from marea.ui.window import MainWindow, qimage_from_skia

log = logging.getLogger("marea")
APP_ID = "ZH3.Marea"


def set_app_id():
    """Identidad propia en la barra de tareas: sin esto, ejecutando con `python -m marea.app` Windows agrupa la
    ventana bajo python.exe y le pone el ícono de Python en vez del nuestro."""
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def install_error_handler(win: MainWindow):
    """Una excepción en un slot no cierra la app (PySide la tiraría en silencio): queda en data/marea.log y sale un aviso."""
    def hook(kind, value, tb):
        log.error("".join(traceback.format_exception(kind, value, tb)))
        win.toast("Algo falló por dentro. Puedes seguir; el detalle quedó en data/marea.log.", "error")
    sys.excepthook = hook


def main() -> int:
    multiprocessing.freeze_support()  # el export corre en un proceso aparte; imprescindible en el .exe empaquetado
    logging.basicConfig(filename=app_data() / "marea.log", level=logging.WARNING, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    set_app_id()  # antes de crear cualquier ventana
    app = QApplication(sys.argv)
    app.setApplicationName("ZH3 Marea")
    app.setOrganizationName("ZH3")
    app.setWindowIcon(theme.app_icon())
    theme.load_fonts()
    settings = make_settings()
    theme.apply(app, theme.saved_name(settings))
    win = MainWindow(settings, cover=qimage_from_skia(demo_cover()))  # portada demo hasta tener reproducción real
    install_error_handler(win)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
