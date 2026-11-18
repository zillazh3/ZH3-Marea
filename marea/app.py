"""Entrada de ZH3 Marea."""
import ctypes
import logging
import multiprocessing
import os
import sys
import traceback

from PySide6.QtWidgets import QApplication

from marea.demo_cover import demo_cover
from marea.paths import app_data, make_settings
from marea.ui import lang, theme
from marea.ui.window import MainWindow, qimage_from_skia
from marea.version import VERSION

log = logging.getLogger("marea")
APP_ID = "ZH3.Marea"


def set_app_id():
    """Identidad propia en la barra de tareas: sin esto, ejecutando con `python -m marea.app` Windows agrupa la
    ventana bajo python.exe y le pone el ícono de Python en vez del nuestro."""
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def set_class_icon(hwnd: int):
    """Explorer asks a just-shown window for its icon while our UI thread is still starting up; that query fails and the
    taskbar falls back to the window-class icon for the whole session. Qt loads that from an `IDI_ICON1` resource the
    PyInstaller exe doesn't have, so it was Windows' generic window icon: give the class our own icon instead."""
    if sys.platform != "win32":
        return
    from ctypes import wintypes
    src = sys.executable if getattr(sys, "frozen", False) else str(theme.ASSETS / "logo" / "marea.ico")
    shell32, user32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("user32")
    shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.UINT]
    user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.SetClassLongPtrW.restype = ctypes.c_void_p
    large, small = ctypes.c_void_p(), ctypes.c_void_p()
    if not shell32.ExtractIconExW(src, 0, ctypes.byref(large), ctypes.byref(small), 1):
        log.warning("No icon found in %s for the taskbar", src)
        return
    for index, icon in ((-14, large), (-34, small)):  # GCLP_HICON, GCLP_HICONSM; the class keeps them for the session
        if icon.value:
            user32.SetClassLongPtrW(hwnd, index, icon)


def install_error_handler(win: MainWindow):
    """Una excepción en un slot no cierra la app (PySide la tiraría en silencio): queda en data/marea.log y sale un aviso."""
    def hook(kind, value, tb):
        log.error("".join(traceback.format_exception(kind, value, tb)))
        win.toast(lang.t("app.unexpectedError"), "error")
    sys.excepthook = hook


def main() -> int:
    multiprocessing.freeze_support()  # el export corre en un proceso aparte; imprescindible en el .exe empaquetado
    logging.basicConfig(filename=app_data() / "marea.log", level=logging.WARNING, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    set_app_id()  # antes de crear cualquier ventana
    app = QApplication(sys.argv)
    app.setApplicationName("ZH3 Marea")
    app.setApplicationVersion(VERSION)
    app.setOrganizationName("ZH3")
    app.setWindowIcon(theme.app_icon())
    theme.load_fonts()
    settings = make_settings()
    theme.apply(app, theme.saved_name(settings))
    lang.apply_preference(os.environ.get("MAREA_LANG") or settings.value("language", "system"))  # MAREA_LANG: dev/QA override
    win = MainWindow(settings, cover=qimage_from_skia(demo_cover()))  # portada demo hasta tener reproducción real
    install_error_handler(win)
    set_class_icon(int(win.winId()))  # before show(): the taskbar reads it the moment the window appears
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
