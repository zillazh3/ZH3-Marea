"""Tema de la app: tokens (color, tipografía, radios, espaciado, movimiento) -> QSS.

Es el único archivo con colores y medidas de estilo: los widgets leen `theme.current` o usan los
selectores del QSS generado. Los íconos (Lucide, SVG con `currentColor`) se recolorean aquí.
"""
import ctypes
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parent.parent.parent / "assets"

FAMILY = "Inter Tight"
FONT_SIZES = {"xs": 12, "sm": 13, "md": 15, "lg": 20, "xl": 28}  # px
RADIUS = {"card": 12, "control": 8, "pill": 999}
SPACE = {1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32}  # escala de 4/8 px
MOTION = {"fast": 150, "normal": 200}  # ms, ease-out
SIDEBAR = {"expanded": 220, "collapsed": 64, "auto_collapse_below": 1200}
BORDER_PX = 1


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str  # fondo de la ventana
    surface: str  # tarjetas
    surface_hover: str
    border: str  # 1 px al 8 %
    text: str
    text_muted: str
    text_faint: str
    pill: str  # sección activa del sidebar
    hover: str
    accent: str  # azul eléctrico
    accent2: str  # violeta
    accent_icon: str  # acento legible sobre el fondo
    danger: str
    on_accent: str  # texto sobre el acento / sobre el rojo de cerrar
    ok: str
    mica_tint: float  # opacidad del color de fondo sobre Mica
    cover_veil: float  # opacidad del velo de colores de la portada


DARK = Theme("dark", bg="#0E0D1A", surface="rgba(255,255,255,0.04)", surface_hover="rgba(255,255,255,0.07)",
             border="rgba(255,255,255,0.08)", text="#F4F3FA", text_muted="rgba(244,243,250,0.62)",
             text_faint="rgba(244,243,250,0.38)", pill="rgba(255,255,255,0.08)", hover="rgba(255,255,255,0.05)",
             accent="#2B56FF", accent2="#6A35FF", accent_icon="#7B93FF", danger="#E5484D", on_accent="#FFFFFF", ok="#3DDC97",
             mica_tint=0.55, cover_veil=0.10)
LIGHT = Theme("light", bg="#F4F4FB", surface="rgba(14,13,26,0.04)", surface_hover="rgba(14,13,26,0.07)",
              border="rgba(14,13,26,0.10)", text="#15142A", text_muted="rgba(21,20,42,0.62)",
              text_faint="rgba(21,20,42,0.38)", pill="rgba(14,13,26,0.08)", hover="rgba(14,13,26,0.05)",
              accent="#2B56FF", accent2="#6A35FF", accent_icon="#2B56FF", danger="#D93036", on_accent="#FFFFFF", ok="#15A06B",
              mica_tint=0.65, cover_veil=0.12)
THEMES = {t.name: t for t in (DARK, LIGHT)}

current: Theme = DARK


class _Signals(QObject):
    changed = Signal()


signals = _Signals()  # los widgets con pintura propia o íconos se conectan a signals.changed


def qc(token: str) -> QColor:
    """Token '#rrggbb' o 'rgba(r,g,b,a)' -> QColor (QSS acepta rgba(), QColor no)."""
    m = re.fullmatch(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", token)
    if m:
        r, g, b, a = m.groups()
        return QColor(int(r), int(g), int(b), round(float(a) * 255))
    return QColor(token)


def reduced_motion() -> bool:
    """True si Windows tiene desactivadas las animaciones (Configuración > Accesibilidad > Efectos visuales)."""
    try:
        flag = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(flag), 0)  # SPI_GETCLIENTAREAANIMATION
        return not flag.value
    except (AttributeError, OSError):
        return False


def duration(kind: str = "normal") -> int:
    return 0 if reduced_motion() else MOTION[kind]


def load_fonts():
    for f in ("Regular", "Medium", "SemiBold", "Bold"):
        QFontDatabase.addApplicationFont(str(ASSETS / "fonts" / f"InterTight-{f}.ttf"))


@lru_cache(maxsize=None)
def _svg(name: str) -> str:
    return (ASSETS / "icons" / f"{name}.svg").read_text(encoding="utf-8")


@lru_cache(maxsize=512)
def icon_pixmap(name: str, color: str, size: int = 20, dpr: float = 1.0) -> QPixmap:
    """Ícono Lucide recoloreado, nítido a la densidad de píxeles dada."""
    svg = _svg(name).replace("currentColor", qc(color).name()).replace('stroke-width="2"', 'stroke-width="1.75"')
    px = QPixmap(round(size * dpr), round(size * dpr))
    px.fill(Qt.transparent)
    p = QPainter(px)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


@lru_cache(maxsize=16)
def logo_pixmap(size: int, dpr: float = 2.0) -> QPixmap:
    px = QPixmap(round(size * dpr), round(size * dpr))
    px.fill(Qt.transparent)
    p = QPainter(px)
    QSvgRenderer(str(ASSETS / "logo" / "marea.svg")).render(p)
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    """Ícono de ventana y barra de tareas: hasta 32 px la versión sólida de 16 px (sin degradado), después la completa."""
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        px = QPixmap(s, s)
        px.fill(Qt.transparent)
        p = QPainter(px)
        QSvgRenderer(str(ASSETS / "logo" / ("marea-16.svg" if s <= 32 else "marea.svg"))).render(p)
        p.end()
        ic.addPixmap(px)
    return ic


def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    return QIcon(icon_pixmap(name, color or current.text_muted, size, 2.0))


def build_qss(t: Theme) -> str:
    f, r = FONT_SIZES, RADIUS
    grad = f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t.accent}, stop:1 {t.accent2})"
    return f"""
* {{ font-family: "{FAMILY}"; font-size: {f['sm']}px; color: {t.text}; outline: none; }}
QWidget#Root, QWidget#Page, QStackedWidget, QLabel {{ background: transparent; }}
QScrollArea, #qt_scrollarea_viewport, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel#H1 {{ font-size: {f['xl']}px; font-weight: 700; }}
QLabel#H2 {{ font-size: {f['lg']}px; font-weight: 600; }}
QLabel#Body {{ font-size: {f['md']}px; color: {t.text_muted}; }}
QLabel#Muted {{ color: {t.text_muted}; }}
QLabel#Caption {{ font-size: {f['xs']}px; color: {t.text_faint}; }}
QLabel#TitleText {{ font-size: {f['sm']}px; font-weight: 500; color: {t.text_muted}; }}
QLabel#SidebarName {{ font-size: {f['md']}px; font-weight: 700; }}
QLabel#PlayerTitle {{ font-size: {f['sm']}px; font-weight: 600; }}
QLabel#PlayerArtist {{ font-size: {f['xs']}px; color: {t.text_muted}; }}
QFrame#Sidebar {{ background: {t.surface}; border: none; border-right: {BORDER_PX}px solid {t.border}; }}
QFrame#Card {{ background: {t.surface}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['card']}px; }}
QFrame#IconTile {{ background: {t.surface}; border: {BORDER_PX}px solid {t.border}; border-radius: 16px; }}
QFrame#Cover {{ background: {t.surface_hover}; border-radius: {r['control']}px; }}
QPushButton {{ border: none; background: transparent; }}
QPushButton#CaptionBtn {{ border-radius: 0; }}
QPushButton#CaptionBtn:hover {{ background: {t.hover}; }}
QPushButton#CaptionBtn[danger="true"]:hover {{ background: {t.danger}; }}
QPushButton#IconBtn {{ border-radius: {r['control']}px; }}
QPushButton#IconBtn:hover {{ background: {t.hover}; }}
QPushButton#Primary {{ background: {grad}; color: {t.on_accent}; border-radius: {r['control']}px;
    padding: {SPACE[2]}px {SPACE[4]}px; font-weight: 600; font-size: {f['sm']}px; }}
QPushButton#Primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t.accent_icon}, stop:1 {t.accent2}); }}
QPushButton#Ghost {{ border: {BORDER_PX}px solid {t.border}; border-radius: {r['control']}px;
    padding: {SPACE[2]}px {SPACE[4]}px; font-weight: 500; }}
QPushButton#Ghost:hover {{ background: {t.hover}; }}
QLineEdit#Search {{ background: {t.surface}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['card']}px;
    padding: {SPACE[3]}px {SPACE[4]}px; font-size: {f['md']}px; selection-background-color: {t.accent}; }}
QLineEdit#Search:focus {{ border: {BORDER_PX}px solid {t.accent}; }}
QComboBox {{ background: {t.surface}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['control']}px;
    padding: {SPACE[2]}px {SPACE[3]}px; min-width: 140px; }}
QComboBox:hover {{ background: {t.surface_hover}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {t.bg}; border: {BORDER_PX}px solid {t.border}; padding: {SPACE[1]}px;
    selection-background-color: {t.pill}; selection-color: {t.text}; outline: none; }}
QToolTip {{ background: {t.bg}; color: {t.text}; border: {BORDER_PX}px solid {t.border};
    border-radius: {r['control']}px; padding: {SPACE[1]}px {SPACE[2]}px; font-size: {f['xs']}px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QSpinBox {{ background: {t.surface}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['control']}px;
    padding: {SPACE[2]}px {SPACE[3]}px; min-width: 80px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QLineEdit, QPlainTextEdit {{ background: {t.surface}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['control']}px;
    padding: {SPACE[2]}px {SPACE[3]}px; selection-background-color: {t.accent}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: {BORDER_PX}px solid {t.accent}; }}
QPushButton::menu-indicator {{ image: none; width: 0; }}
QPushButton#Primary:disabled {{ background: {t.surface_hover}; color: {t.text_faint}; }}
QPushButton#Ghost:disabled {{ color: {t.text_faint}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: {SPACE[2]}px {SPACE[3]}px; color: {t.text_muted}; border: none;
    border-bottom: 2px solid transparent; font-weight: 500; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
QCheckBox {{ spacing: {SPACE[2]}px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: {BORDER_PX}px solid {t.text_faint};
    background: {t.surface}; }}
QCheckBox::indicator:checked {{ background: {grad}; border: {BORDER_PX}px solid {t.accent}; }}
QSlider::groove:horizontal {{ height: 4px; background: {t.surface_hover}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {grad}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: {t.text}; }}
QProgressBar {{ background: {t.surface_hover}; border: none; border-radius: 4px; max-height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {grad}; border-radius: 4px; }}
QMenu {{ background: {t.bg}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['control']}px; padding: {SPACE[1]}px; }}
QMenu::item {{ padding: {SPACE[2]}px {SPACE[5]}px {SPACE[2]}px {SPACE[3]}px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.pill}; }}
QMenu::item:disabled {{ color: {t.text_faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: {SPACE[1]}px {SPACE[2]}px; }}
QListWidget {{ background: transparent; border: none; }}
QDialog, QMessageBox {{ background: {t.bg}; }}
QFrame#Toast {{ background: {t.bg}; border: {BORDER_PX}px solid {t.border}; border-radius: {r['card']}px; }}
QFrame#Toast[kind="error"] {{ border: {BORDER_PX}px solid {t.danger}; }}
QFrame#Toast[kind="ok"] {{ border: {BORDER_PX}px solid {t.ok}; }}
"""


def apply(app, name: str):
    global current
    current = THEMES[name]
    app.setStyleSheet(build_qss(current))
    signals.changed.emit()


def saved_name(settings: QSettings) -> str:
    n = settings.value("theme", "dark")
    return n if n in THEMES else "dark"
