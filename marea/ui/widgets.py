from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLabel

# tipo de letra disponible (el valor viene de Lyrics.kind o 'none'/'error')
LYRICS_BADGES = {"syllable": "Sílaba", "line": "Línea", "none": "Sin letra", "error": "—"}


def placeholder_pixmap(size: int, radius: int, icon_px: int = 24, dpr: float = 2.0) -> QPixmap:
    """Recuadro de portada vacía con un disco centrado, a tamaño nativo (sin reescalar el ícono)."""
    from marea.ui import theme
    px = QPixmap(round(size * dpr), round(size * dpr))
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(theme.qc(theme.current.surface_hover))
    p.drawRoundedRect(QRectF(0, 0, size * dpr, size * dpr), radius * dpr, radius * dpr)
    off = round((size - icon_px) / 2 * dpr)
    p.drawPixmap(off, off, theme.icon_pixmap("disc-3", theme.current.text_faint, icon_px, dpr))
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


def rounded_pixmap(source: QPixmap, size: int, radius: int, dpr: float = 2.0) -> QPixmap:
    """Portada cuadrada recortada con esquinas redondeadas, nítida a la densidad de píxeles dada."""
    px = size * dpr
    scaled = source.scaled(round(px), round(px), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(round(px), round(px))
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, px, px), radius * dpr, radius * dpr)
    p.setClipPath(clip)
    p.drawPixmap(round((px - scaled.width()) / 2), round((px - scaled.height()) / 2), scaled)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


class ElidedLabel(QLabel):
    """QLabel de una línea que recorta con «…» según el ancho disponible."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setMinimumWidth(40)
        self.setText(text)

    def set_full_text(self, text: str):
        self._full = text
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(0, self.width())))
