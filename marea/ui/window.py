"""Ventana principal sin marco nativo: barra de título propia, sidebar, pantallas, Mica y borde redimensionable."""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from marea.async_util import run_async
from marea.export.queue import ExportQueue
from marea.paths import make_settings, reveal_in_folder
from marea.project import ClipProject, ProjectError, demo_project, load_project_file, project_from_ttml
from marea.providers.errors import ProviderError
from marea.providers.spotify import Track
from marea.ui import lang, theme
from marea.ui.editor import EditorPage, project_filter
from marea.ui.exports import ExportsPage
from marea.ui.home import HomePage
from marea.ui.library import LibraryPage
from marea.ui.palette import CommandPalette
from marea.ui.search import SearchPage
from marea.ui.services import Services
from marea.ui.settings import SettingsBody
from marea.ui.sidebar import Sidebar
from marea.ui.toast import ToastHost
from marea.ui.widgets import ElidedLabel, FadeStack, Page, scrollable

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
        self.title.setTextFormat(Qt.PlainText)  # the song title comes from Spotify: never render it as HTML
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
        self._project_title: str | None = None
        self._playing_title: str | None = None
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
        self.editor.project_changed.connect(self._on_editor_project_changed)
        self.editor.enqueued.connect(
            lambda _: self.toast(lang.t("window.queued"), "info", (lang.t("window.viewQueue"), lambda: self._goto_page(self.page_exports)))
        )
        self.queue.item_done.connect(lambda it: self.toast(lang.t("window.exportDone", name=it.out_path.name), "ok",
                                                           (lang.t("common.openFolder"), lambda: reveal_in_folder(str(it.out_path)))))
        self.queue.item_failed.connect(lambda it: self.toast(lang.t("window.exportFailed", title=it.title, error=it.error), "error"))
        self.settings_body = SettingsBody(self.settings, self.set_theme, self.services, self.toast)
        self.settings_scroll = scrollable(self.settings_body)
        self.page_home = HomePage(
            self.services, self.open_track, self.open_ttml, self.open_project_dialog, self.load_demo,
            lambda: self._goto_page(self.page_search), self.open_project_file,
            lambda: self._goto_page(self.page_settings), self.queue,
        )
        self.editor.library_changed.connect(self.page_home.reload_recents)
        self.page_search = SearchPage(self.services, self.open_track, lambda: self._goto_page(self.page_settings), self.toast)
        self.page_library = LibraryPage(self.open_project_file, lambda: self._goto_page(self.page_home))
        self.editor.library_changed.connect(self.page_library.reload)
        self.queue.item_done.connect(lambda _: self.page_library.reload())
        self.page_editor = self.editor
        self.page_exports = ExportsPage(self.queue)
        self.page_settings = Page("", self.settings_scroll, framed=False)
        lang.bind(self.page_settings.h1, "nav.settings")
        # mismo orden que ITEMS del sidebar: el índice del botón es el de la página
        for p in (self.page_home, self.page_search, self.page_editor, self.page_library, self.page_exports, self.page_settings):
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
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.open_palette).setContext(Qt.ApplicationShortcut)
        self.sidebar.palette_requested.connect(self.open_palette)
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
    def _goto_page(self, page: QWidget):
        idx = self.pages.indexOf(page)
        if idx >= 0:
            self.sidebar.select(idx)
            self.pages.set_page(idx)

    def open_lab_model_settings(self, model_id: str):
        self._goto_page(self.page_settings)
        target = self.settings_body.focus_lab_model(model_id)
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, lambda: (self.settings_scroll.ensureWidgetVisible(target), target.setFocus()))

    def _on_editor_project_changed(self, title: str | None):
        self.set_song(title)
        if self.editor.project is not None:
            self.set_cover(qimage_from_skia(self.editor.project.cover))

    def set_song(self, title: str | None):
        self._project_title = title
        self._refresh_title()

    def _refresh_title(self):
        """Title bar: the open project wins, then the song playing on Spotify, then the app name."""
        self.titlebar.title.set_full_text(self._project_title or self._playing_title or APP_NAME)

    def toast(self, text: str, kind: str = "info", action=None):
        return self.toasts.show_toast(text, kind, action)

    def open_ttml(self):
        path, _ = QFileDialog.getOpenFileName(
            self, lang.t("window.openLyricsTitle"), "", lang.t("window.openLyricsFilter")
        )
        if not path:
            return
        if path.lower().endswith(".json"):
            self.open_project_file(path)
            return
        try:
            self.load_project(project_from_ttml(path))
        except (ValueError, OSError) as e:
            self.toast(lang.t("window.openLyricsFailed", error=e), "error")

    def open_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, lang.t("window.openProjectTitle"), "", project_filter())
        if path:
            self.open_project_file(path)

    def open_project_file(self, path: str | Path) -> bool:
        try:
            project, state = load_project_file(path)
        except (ProjectError, OSError) as e:
            self.toast(str(e), "error")
            return False
        self.load_project(project, editor_state=state)
        self.editor.register_in_library()
        return True

    def open_track(self, track: Track):
        """Una canción de Spotify: busca su letra y portada y abre el Editor."""
        self.toast(lang.t("window.preparing", title=track.title))
        run_async(lambda: self.services.build_project(track), self.load_project,
                  lambda e: self.toast(str(e) if isinstance(e, ProviderError) else lang.t("window.prepareFailed"), "error"))

    def _on_playback(self, pb):
        t = pb.track if pb else None
        self.sidebar.player.set_track(t.title if t else None, t.artist if t else None, bool(pb and pb.is_playing))
        self._playing_title = f"{t.title} — {t.artist}" if t and t.artist else (t.title if t else None)
        self._refresh_title()

    def load_demo(self):
        self.load_project(demo_project())

    def load_project(self, project: ClipProject, editor_state: dict | None = None):
        self.set_cover(qimage_from_skia(project.cover))
        self.editor.set_project(project, editor_state=editor_state)
        self._goto_page(self.page_editor)

    def set_cover(self, cover: QImage):
        """Velo muy sutil con los colores de la portada actual (tapa ya borrosa: 8 px escalados suavemente)."""
        self._veil = cover.scaled(8, 8, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.update()

    def set_theme(self, name: str):
        self.settings.setValue("theme", name)
        theme.apply(QApplication.instance(), name)

    def toggle_sidebar(self):
        self.sidebar.set_collapsed(not self.sidebar.collapsed)

    def palette_commands(self) -> list[tuple[str, str, object]]:
        go = lambda page: (lambda: self._goto_page(page))  # noqa: E731
        screens = [(lang.t("nav.home"), self.page_home), (lang.t("nav.search"), self.page_search),
                   (lang.t("nav.editor"), self.page_editor), (lang.t("nav.library"), self.page_library),
                   (lang.t("nav.exports"), self.page_exports), (lang.t("nav.settings"), self.page_settings)]
        combo = self.settings_body.combo
        action = lang.t("palette.groupAction")
        return [(lang.t("palette.groupGo"), name, go(page)) for name, page in screens] + [
            (action, lang.t("palette.searchSong"), go(self.page_search)),
            (action, lang.t("palette.toggleTheme"), lambda: combo.setCurrentIndex(1 - combo.currentIndex())),
            (action, lang.t("palette.toggleSidebar"), self.toggle_sidebar),
            (action, lang.t("palette.openTtml"), self.open_ttml),
            (action, lang.t("palette.openProject"), self.open_project_dialog),
            (action, lang.t("palette.tryDemo"), self.load_demo),
        ]

    def open_palette(self):
        CommandPalette(self, self.palette_commands()).popup()

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
        if e.type() == QEvent.ActivationChange and self.isActiveWindow() and hasattr(self, "settings_body"):
            self.settings_body.refresh_lab_status()
        super().changeEvent(e)

    def closeEvent(self, e):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("sidebar_collapsed", self._pref_collapsed)
        self.settings.sync()  # no esperar a que se destruya el objeto: un cierre brusco perdería las preferencias
        self.queue.shutdown()  # lo que se esté exportando se cancela: sin mp4 a medias ni Spotify sonando
        self.editor.shutdown()
        if close_services := getattr(self.services, "close", None):
            close_services()
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
