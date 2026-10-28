"""Preview 9:16 en vivo: mismo Scene y mismo motor que el export, a paso fijo de 1/60 s y resolución reducida."""
from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from marea.engine.engine import Params
from marea.render.gpu import make_context
from marea.render.scene import Scene, Style
from marea.ui import theme

DT = 1 / 60
MAX_STEPS = 4  # si la ventana se congela, no intentar «recuperar» segundos de simulación de golpe
SETTLE_MS = 1500  # tras un cambio estando en pausa se sigue dibujando hasta que los springs se asientan


class PreviewWidget(QWidget):
    position = Signal(float, float)  # ms dentro del rango, duración del rango (ms)
    playing_changed = Signal(bool)

    def __init__(self, parent=None, prefer_gpu: bool = True):
        super().__init__(parent)
        self.setMinimumSize(180, 320)
        self._prefer_gpu, self.ctx, self.scene, self._surface = prefer_gpu, None, None, None
        self._project, self._style, self._params = None, Style(), Params()
        self._range = (0.0, 1.0)
        self.playing, self._frame, self._image = False, None, None
        self._acc, self._settle = 0.0, 0.0
        self._clock = QElapsedTimer()
        self._timer = QTimer(self, interval=8, timerType=Qt.PreciseTimer)  # el tick real lo marca el reloj, no el timer
        self._timer.timeout.connect(self._tick)

    # ---- configuración ----
    def set_project(self, project, style: Style, params: Params, range_ms: tuple[float, float]):
        self._project, self._style, self._params = project, style, params
        self._range = range_ms
        self.rebuild()

    def rebuild(self):
        """Escena nueva (cambió la portada, la fuente o la letra); conserva la posición."""
        if not self._project:
            return
        offset = self.scene.engine.t * 1000 - self._range[0] if self.scene else 0.0
        if self.ctx is None:
            self.ctx = make_context(self._prefer_gpu)
        size = (540, 960) if self.ctx.gpu else (270, 480)  # en CPU, menos resolución para que siga fluida
        self.scene = Scene(self._project.lyrics, self._project.cover, size, self._params, self._style, gpu=self.ctx.gpu)
        self._surface = self.ctx.make_surface(*size)
        self.seek(offset)

    def set_range(self, range_ms: tuple[float, float]):
        self._range = range_ms
        self.seek(0)

    def refresh(self):
        """Cambió algo solo visual (título, bloom): volver a dibujar aunque esté en pausa."""
        if self.scene:
            self._wake()

    def restyle(self):
        """Cambiaron parámetros del motor o del estilo: re-layout sin cortar la animación."""
        if self.scene:
            self.scene.engine.reconfigure()
            self._wake()

    # ---- reproducción ----
    @property
    def duration(self) -> float:
        return max(1.0, self._range[1] - self._range[0])

    def seek(self, offset_ms: float):
        """Salta a `offset_ms` desde el inicio del rango, con el motor asentado (pre-roll de 3 s)."""
        if not self.scene:
            return
        offset_ms = min(max(0.0, offset_ms), self.duration)
        self.scene.engine.warmup(self._range[0] + offset_ms)
        self._render()
        self.position.emit(offset_ms, self.duration)
        self._wake()

    def set_playing(self, on: bool):
        if on == self.playing or (on and not self.scene):
            return
        self.playing = on
        self._clock.restart()
        self._wake()
        self.playing_changed.emit(on)

    def _wake(self):
        self._settle = SETTLE_MS
        if not self._timer.isActive():
            self._clock.restart()
            self._acc = 0.0
            self._timer.start()

    def _tick(self):
        self._acc += self._clock.restart() / 1000
        e = self.scene.engine
        steps = 0
        while self._acc >= DT and steps < MAX_STEPS:
            e.advance(DT, running=self.playing)
            self._acc -= DT
            steps += 1
        self._acc = min(self._acc, DT)
        if not steps:
            return
        if self.playing and e.t * 1000 >= self._range[1]:
            self.seek(0)  # el clip se repite
            return
        self._settle = SETTLE_MS if self.playing else self._settle - steps * DT * 1000
        self._render()
        self.position.emit(e.t * 1000 - self._range[0], self.duration)
        if not self.playing and self._settle <= 0:
            self._timer.stop()

    def _render(self):
        canvas = self._surface.getCanvas()
        self.scene.clip_start_ms = self._range[0]
        self.scene.render(canvas)
        self._frame = self.ctx.read_rgba(self._surface)
        h, w = self._frame.shape[:2]
        self._image = QImage(self._frame.data, w, h, w * 4, QImage.Format_RGBA8888)
        self.update()

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    def shutdown(self):
        self._timer.stop()
        if self.ctx:
            self.ctx.close()
            self.ctx = None

    # ---- pintura ----
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        pw = min(w, h * 9 / 16)
        box = QRectF((w - pw) / 2, (h - pw * 16 / 9) / 2, pw, pw * 16 / 9)
        clip = QPainterPath()
        clip.addRoundedRect(box, theme.RADIUS["card"], theme.RADIUS["card"])
        p.setClipPath(clip)
        if self._image:
            p.drawImage(box, self._image)
        else:
            p.fillRect(box, theme.qc(theme.current.surface))
