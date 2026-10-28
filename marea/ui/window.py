"""Ventana principal sin marco nativo: barra de título propia, sidebar, pantallas, Mica y borde redimensionable."""
import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QEvent, QPoint, QSettings, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from marea.export.queue import ExportQueue
from marea.paths import make_settings, reveal_in_folder
from marea.project import ClipProject, demo_project, project_from_ttml
from marea.providers.errors import ProviderError
from marea.providers.spotify import Track
from marea.ui import theme
from marea.ui.editor import EditorPage
from marea.ui.exports import ExportsPage
from marea.ui.home import HomePage
from marea.ui.net import run_async
from marea.ui.screens import EmptyState, FadeStack, Page, SettingsBody, scrollable
from marea.ui.search import SearchPage
from marea.ui.services import Services
from marea.ui.sidebar import Sidebar
from marea.ui.toast import ToastHost
from marea.ui.widgets import ElidedLabel

TITLE_H = 40
EDGE = 6  # px de borde que redimensiona
MIN_SIZE = (1100, 700)
DEFAULT_SIZE = (1280, 800)
APP_NAME = "ZH3 Marea"


# ---------------- Mica (Windows 11) ----------------

class _MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


def mica_supported() -> bool:
    return sys.platform == "win32" and sys.getwindowsversion().build >= 22621  # backdrop types: Windows 11 22H2+


def apply_mica(hwnd: int, dark: bool) -> bool:
    """Mica + esquinas redondeadas + barra en modo oscuro vía DWM. False si el sistema no lo admite."""
    if not mica_supported():
        return False
    dwm = ctypes.windll.dwmapi
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    h = wintypes.HWND(hwnd)

    def attr(a: int, v: int) -> int:
        val = ctypes.c_int(v)
        return dwm.DwmSetWindowAttribute(h, a, ctypes.byref(val), 4)

    attr(20, int(dark))  # DWMWA_USE_IMMERSIVE_DARK_MODE
    attr(33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
    ok = attr(38, 2) == 0  # DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_MAINWINDOW (Mica)
    margins = _MARGINS(-1, -1, -1, -1)
    return ok and dwm.DwmExtendFrameIntoClientArea(h, ctypes.byref(margins)) == 0


# ---------------- Barra de título ----------------

class CaptionButton(QPushButton):
    def __init__(self, icon_name: str, danger: bool = False):
        super().__init__()
        self.icon_name, self._hover = icon_name, False
        self.setObjectName("CaptionBtn")
        self.setProperty("danger", danger)
        self.setFixedSize(46, TITLE_H)
        self.setFocusPolicy(Qt.NoFocus)
        theme.signals.changed.connect(self._refresh)
        self._refresh()

    def set_icon_name(self, name: str):
        self.icon_name = name
        self._refresh()

    def _refresh(self):
        white = self._hover and self.property("danger")
        self.setIcon(theme.icon(self.icon_name, theme.current.on_accent if white else theme.current.text_muted, 14))

    def enterEvent(self, e):
        self._hover = True
        self._refresh()

    def leaveEvent(self, e):
        self._hover = False
        self._refresh()


class TitleBar(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setFixedHeight(TITLE_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[3], 0, 0, 0)
        lay.setSpacing(theme.SPACE[2])
        self.logo = QLabel()
        self.logo.setPixmap(theme.logo_pixmap(18, self.devicePixelRatioF()))
        self.title = ElidedLabel(APP_NAME)
        self.title.setObjectName("TitleText")
        self.btn_min, self.btn_max, self.btn_close = CaptionButton("minus"), CaptionButton("square"), CaptionButton("x", True)
        self.btn_min.clicked.connect(win.showMinimized)
        self.btn_max.clicked.connect(win.toggle_maximize)
        self.btn_close.clicked.connect(win.close)
        lay.addWidget(self.logo)
        lay.addWidget(self.title, 1)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.win.windowHandle().startSystemMove()  # arrastre y snap de Windows 11

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.win.toggle_maximize()


# ---------------- Ventana ----------------

def qimage_from_skia(img) -> QImage:
    import skia
    a = img.toarray(colorType=skia.kRGBA_8888_ColorType)
    return QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_RGBA8888).copy()


class MainWindow(QWidget):
    def __init__(self, settings: QSettings | None = None, mica: bool = True, cover: QImage | None = None,
                 gpu: bool = True, services: Services | None = None):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint)
        self.setObjectName("Root")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.setMinimumSize(*MIN_SIZE)
        self.settings = settings or make_settings()
        self._mica_wanted = mica and mica_supported()
        self._mica = False
        if self._mica_wanted:
            self.setAttribute(Qt.WA_TranslucentBackground)  # Mica se ve a través de la ventana
        self._veil: QImage | None = None
        self._narrow = False
        self._pref_collapsed = self.settings.value("sidebar_collapsed", False, type=bool)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.titlebar = TitleBar(self)
        root.addWidget(self.titlebar)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.sidebar = Sidebar()
        self.pages = FadeStack()
        self.services = services or Services(self.settings)
        self.queue = ExportQueue(self.services, self)
        self.editor = EditorPage(self.settings, self.toast, self.queue, self.services, prefer_gpu=gpu)
        self.editor.project_changed.connect(self.set_song)
        goto = lambda i: (self.sidebar.select(i), self.pages.set_page(i))
        self.editor.enqueued.connect(lambda _: self.toast("Añadido a la cola de exportaciones.", "info", ("Ver cola", lambda: goto(4))))
        self.queue.item_done.connect(lambda it: self.toast(f"Listo: {it.out_path.name}", "ok",
                                                           ("Abrir carpeta", lambda: reveal_in_folder(str(it.out_path)))))
        self.queue.item_failed.connect(lambda it: self.toast(f"No pude exportar «{it.title}»: {it.error}", "error"))
        for p in (HomePage(self.services, self.open_track, self.open_ttml, self.load_demo, lambda: goto(5), self.queue),
                  SearchPage(self.services, self.open_track, lambda: goto(5), self.toast),
                  Page("Biblioteca", EmptyState("library", "Tu biblioteca está vacía", "Aquí aparecerán las letras y los clips que guardes.")),
                  self.editor,
                  ExportsPage(self.queue),
                  Page("Ajustes", scrollable(SettingsBody(self.settings, self.set_theme, self.services, self.toast)))):
            self.pages.addWidget(p)
        body.addWidget(self.sidebar)
        body.addWidget(self.pages, 1)
        root.addLayout(body, 1)
        self.toasts = ToastHost(self)

        self.sidebar.navigated.connect(self.pages.set_page)
        self.services.connection_changed.connect(self.sidebar.spotify.set_connected)
        self.services.now_playing.connect(self._on_playback)
        self.services.problem.connect(lambda msg: self.toast(msg, "error"))
        player = self.sidebar.player
        player.prev_clicked.connect(lambda: self.services.control("previous"))
        player.play_clicked.connect(lambda: self.services.control("toggle"))
        player.next_clicked.connect(lambda: self.services.control("next"))
        self.sidebar.spotify.set_connected(self.services.connected)
        self.sidebar.collapsed_changed.connect(lambda c: self._remember_collapse(c))
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.toggle_sidebar).setContext(Qt.ApplicationShortcut)
        theme.signals.changed.connect(self._theme_changed)
        QApplication.instance().installEventFilter(self)
        if cover is not None:
            self.set_cover(cover)

        geo = self.settings.value("geometry")
        if not (geo and self.restoreGeometry(geo)):
            self.resize(*DEFAULT_SIZE)
        self._narrow = self.width() < theme.SIDEBAR["auto_collapse_below"]
        self.sidebar.set_collapsed(self._pref_collapsed or self._narrow, animate=False)

    # ---- API ----
    def set_song(self, title: str | None):
        self.titlebar.title.set_full_text(title or APP_NAME)

    def toast(self, text: str, kind: str = "info", action=None):
        return self.toasts.show_toast(text, kind, action)

    def open_ttml(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir letra", "", "Letras TTML (*.ttml *.xml)")
        if not path:
            return
        try:
            self.load_project(project_from_ttml(path))
        except (ValueError, OSError) as e:
            self.toast(f"No pude abrir esa letra: {e} Usa un .ttml de Apple Music con tiempos por sílaba.", "error")

    def open_track(self, track: Track):
        """Una canción de Spotify: busca su letra y portada y abre el Editor."""
        self.toast(f"Preparando «{track.title}»…")
        run_async(lambda: self.services.build_project(track), self.load_project,
                  lambda e: self.toast(str(e) if isinstance(e, ProviderError) else "No pude preparar esa canción. Intenta de nuevo.", "error"))

    def _on_playback(self, pb):
        t = pb.track if pb else None
        self.sidebar.player.set_track(t.title if t else None, t.artist if t else None, bool(pb and pb.is_playing))

    def load_demo(self):
        self.load_project(demo_project())

    def load_project(self, project: ClipProject):
        self.set_cover(qimage_from_skia(project.cover))
        self.editor.set_project(project)
        self.sidebar.select(3)
        self.pages.set_page(3)

    def set_cover(self, cover: QImage):
        """Velo muy sutil con los colores de la portada actual (tapa ya borrosa: 8 px escalados suavemente)."""
        self._veil = cover.scaled(8, 8, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.update()

    def set_theme(self, name: str):
        self.settings.setValue("theme", name)
        theme.apply(QApplication.instance(), name)

    def toggle_sidebar(self):
        self.sidebar.set_collapsed(not self.sidebar.collapsed)

    def toggle_maximize(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    # ---- estado ----
    def _remember_collapse(self, collapsed: bool):
        if not self._narrow or not collapsed:  # el auto-colapso por ancho no cambia la preferencia del usuario
            self._pref_collapsed = collapsed

    def _theme_changed(self):
        if self._mica:
            apply_mica(int(self.winId()), theme.current.name == "dark")
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self._mica_wanted and not self._mica:
            self._mica = apply_mica(int(self.winId()), theme.current.name == "dark")
            self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.toasts.resize_to(self.size())
        narrow = self.width() < theme.SIDEBAR["auto_collapse_below"]
        if narrow != self._narrow:  # solo al cruzar el umbral: así el usuario puede reabrirlo estando angosto
            self._narrow = narrow
            self.sidebar.set_collapsed(True if narrow else self._pref_collapsed)

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            self.titlebar.btn_max.set_icon_name("copy" if self.isMaximized() else "square")
        super().changeEvent(e)

    def closeEvent(self, e):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("sidebar_collapsed", self._pref_collapsed)
        self.settings.sync()  # no esperar a que se destruya el objeto: un cierre brusco perdería las preferencias
        self.queue.shutdown()  # lo que se esté exportando se cancela: sin mp4 a medias ni Spotify sonando
        self.editor.shutdown()
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(e)

    def paintEvent(self, e):
        t, p = theme.current, QPainter(self)
        bg = theme.qc(t.bg)
        if self._mica:  # el fondo deja pasar Mica y solo lo entinta
            bg.setAlphaF(t.mica_tint)
            p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), bg)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self._veil:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setOpacity(t.cover_veil)
            p.drawImage(self.rect(), self._veil)

    # ---- bordes redimensionables (startSystemResize) ----
    def _edges(self, pos: QPoint) -> Qt.Edges:
        e = Qt.Edges()
        if pos.x() <= EDGE:
            e |= Qt.LeftEdge
        if pos.x() >= self.width() - EDGE:
            e |= Qt.RightEdge
        if pos.y() <= EDGE:
            e |= Qt.TopEdge
        if pos.y() >= self.height() - EDGE:
            e |= Qt.BottomEdge
        return e

    def eventFilter(self, obj, ev):
        if (ev.type() in (QEvent.MouseMove, QEvent.MouseButtonPress) and isinstance(obj, QWidget)
                and obj.window() is self and not (self.isMaximized() or self.isFullScreen())):
            edges = self._edges(self.mapFromGlobal(ev.globalPosition().toPoint()))
            if ev.type() == QEvent.MouseButtonPress:
                if ev.button() == Qt.LeftButton and edges:
                    self.windowHandle().startSystemResize(edges)
                    return True
            elif not ev.buttons():
                self._set_resize_cursor(edges)
        return False

    def _set_resize_cursor(self, edges: Qt.Edges):
        horiz = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vert = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        diag_back = (edges & Qt.LeftEdge and edges & Qt.TopEdge) or (edges & Qt.RightEdge and edges & Qt.BottomEdge)
        if horiz and vert:
            self.setCursor(Qt.SizeFDiagCursor if diag_back else Qt.SizeBDiagCursor)
        elif horiz:
            self.setCursor(Qt.SizeHorCursor)
        elif vert:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
