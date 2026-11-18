"""Forma de onda del audio local frente a los versos, para ajustar el desfase a ojo."""
import numpy as np
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget

from marea.audio import local
from marea.ui import theme


class WaveformWidget(QWidget):
    """Eje = tiempo de la letra. El audio se dibuja desplazado por `offset_ms`: al moverlo, los golpes de la forma de onda
    deben coincidir con las líneas de los versos (marcadores)."""

    seek_requested = Signal(float)  # song_ms clamped to active clip range
    drag_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(88)
        self._samples = None
        self._view = (0.0, 1.0)
        self._offset = 0.0
        self._markers: list[float] = []
        self._range = (0.0, 1.0)
        self._playhead_ms: float | None = None
        self._dragging = False
        self._cache = None

    @property
    def dragging(self) -> bool:
        return self._dragging

    def set_data(self, samples, view_ms: tuple[float, float], offset_ms: float, markers_ms: list[float],
                 range_ms: tuple[float, float]):
        self._samples, self._view, self._offset, self._markers, self._range = samples, view_ms, offset_ms, markers_ms, range_ms
        self._cache = None
        self.setCursor(Qt.PointingHandCursor if samples is not None else Qt.ArrowCursor)
        self.update()

    def set_playhead(self, song_ms: float | None):
        """Update playhead marker without invalidating cached waveform peaks."""
        if song_ms != self._playhead_ms:
            self._playhead_ms = song_ms
            self.update()

    def peaks(self, bins: int) -> np.ndarray:
        key = (bins, self._view, self._offset, id(self._samples))
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        a = round((self._view[0] + self._offset) / 1000 * local.RATE)
        b = round((self._view[1] + self._offset) / 1000 * local.RATE)
        n = b - a
        block = np.zeros((n, 2), np.float32)  # fuera del archivo, silencio
        lo, hi = max(a, 0), min(b, len(self._samples))
        if hi > lo:
            block[lo - a:hi - a] = self._samples[lo:hi]
        self._cache = (key, local.waveform(block, bins))
        return self._cache[1]

    def _x(self, ms: float) -> float:
        span = max(1e-6, self._view[1] - self._view[0])
        return (ms - self._view[0]) / span * self.width()

    def _ms_at(self, x: float) -> float:
        span = self._view[1] - self._view[0]
        raw = self._view[0] + max(0.0, min(1.0, x / max(1, self.width()))) * span
        return max(self._range[0], min(self._range[1], raw))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._samples is not None:
            self._dragging = True
            self.seek_requested.emit(self._ms_at(e.position().x()))
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._dragging and self._samples is not None:
            self.seek_requested.emit(self._ms_at(e.position().x()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.drag_finished.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        t, p = theme.current, QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc(t.surface))
        p.drawRoundedRect(r, theme.RADIUS["control"], theme.RADIUS["control"])
        if self._samples is None or self.width() < 10:
            return
        x0, x1 = self._x(self._range[0]), self._x(self._range[1])
        p.setBrush(theme.qc(t.pill))
        p.drawRect(QRectF(max(x0, 0), 0, min(x1, self.width()) - max(x0, 0), r.height()))
        p.setPen(theme.qc(t.accent))
        for m in self._markers:  # inicio de cada verso: detrás, para que no tape los golpes que coinciden con él
            x = int(self._x(m))
            if 0 <= x <= self.width():
                p.drawLine(x, 0, x, int(r.height()))
        mid, half = r.height() / 2, r.height() / 2 - 8
        peaks = np.minimum(self.peaks(self.width() // 3), 1.0)
        step = self.width() / len(peaks)
        pen = p.pen()
        pen.setWidthF(max(1.5, step * 0.7))
        for i, v in enumerate(peaks):
            x = i * step + step / 2
            pen.setColor(theme.qc(t.accent_icon if x0 <= x <= x1 else t.text_muted))
            p.setPen(pen)
            p.drawLine(int(x), int(mid - max(1.0, v * half)), int(x), int(mid + max(1.0, v * half)))
        if self._playhead_ms is not None:
            px = self._x(self._playhead_ms)
            if 0 <= px <= self.width():
                head_pen = p.pen()
                head_pen.setColor(theme.qc(t.text))
                head_pen.setWidthF(1.75)
                p.setPen(head_pen)
                p.drawLine(int(round(px)), 2, int(round(px)), int(r.height() - 2))
