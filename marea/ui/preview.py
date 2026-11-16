"""Preview 9:16 en vivo: mismo Scene y mismo motor que el export, a paso fijo de 1/60 s y resolución reducida."""
from time import perf_counter

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QImage, QKeySequence, QPainter, QPainterPath, QShortcut
from PySide6.QtWidgets import QWidget

from marea.engine.engine import Params
from marea.render.gpu import make_context
from marea.render.scene import Scene, Style
from marea.ui import theme

DT = 1 / 60
MAX_STEPS = 4  # si la ventana se congela, no intentar «recuperar» segundos de simulación de golpe
SETTLE_MS = 1500  # tras un cambio estando en pausa se sigue dibujando hasta que los springs se asientan
TICK_MS = 8  # el paso real lo marca el reloj; con 16 ms y DT=16.67 ms el temporizador se salta pasos y el scroll avanza a tirones


class PreviewWidget(QWidget):
    position = Signal(float, float)  # ms dentro del rango, duración del rango (ms)
    playing_changed = Signal(bool)
    _video_frame_ready = Signal()

    def __init__(self, parent=None, prefer_gpu: bool = True, async_video: bool = True):
        super().__init__(parent)
        self.setMinimumSize(180, 320)
        self._prefer_gpu, self._async_video = prefer_gpu, async_video
        self.ctx, self.scene, self._surface = None, None, None
        self._project, self._style, self._params = None, Style(), Params()
        self._range = (0.0, 1.0)
        self._wordfx, self._wordfx_debug = None, False  # experimental: plan de efectos por palabra y su depuración (solo preview)
        self.playing, self._frame, self._image = False, None, None
        self._acc, self._settle = 0.0, 0.0
        self._clock = QElapsedTimer()
        self._debug, self._frame_ms, self._frame_avg = False, 0.0, 0.0  # overlay F3 (solo preview, nunca en el export)
        self._lag0: tuple[float, float] | None = None  # (reloj de pared, tiempo de la simulación) al empezar a reproducir
        QShortcut(QKeySequence("F3"), self, activated=self.toggle_debug)
        self._timer = QTimer(self, interval=TICK_MS, timerType=Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._video_frame_ready.connect(self._on_video_frame_ready, Qt.QueuedConnection)

    # ---- configuración ----
    def set_project(self, project, style: Style, params: Params, range_ms: tuple[float, float]):
        self._project, self._style, self._params = project, style, params
        self._range = range_ms
        self.rebuild()

    def update_wordfx(self, plan, debug: bool = False):
        """Actualiza Semantic Word FX en vivo sobre la escena actual sin reconstruir springs ni layout."""
        self._wordfx, self._wordfx_debug = plan, debug
        if self.scene and self._project is not None:
            self.scene.attach_wordfx(self._project.lyrics, plan)
            self.scene.wordfx_debug = debug
            if not self.playing:
                self._render()
            self._wake()

    def set_wordfx(self, plan, debug: bool = False, rebuild: bool = True):
        """Plan de Semantic Word FX (None = apagado). Actualiza la escena en vivo si la letra no cambió."""
        self._wordfx, self._wordfx_debug = plan, debug
        if not rebuild:
            return
        if (self.scene is not None and self._project is not None
                and self.scene.matches_lyrics(self._project.lyrics)):
            self.update_wordfx(plan, debug)
            return
        self.rebuild()

    def rebuild(self):
        """Escena nueva (cambió la portada, la fuente o la letra); conserva la posición."""
        if not self._project:
            return
        offset = self.scene.engine.t * 1000 - self._range[0] if self.scene else 0.0
        if self.scene is not None:
            self.scene.close()
        if self.ctx is None:
            self.ctx = make_context(self._prefer_gpu)
        size = (540, 960) if self.ctx.gpu else (270, 480)  # en CPU, menos resolución para que siga fluida
        self.scene = Scene(
            self._project.lyrics,
            self._project.cover,
            size,
            self._params,
            self._style,
            gpu=self.ctx.gpu,
            cinematic=getattr(self._project, "cinematic", None),
            async_video=self._async_video,
            on_video_frame=self._video_frame_ready.emit,
            wordfx=self._wordfx,
            wordfx_debug=self._wordfx_debug,
        )
        self._surface = self.ctx.make_surface(*size)
        self.seek(offset)

    def set_range(self, range_ms: tuple[float, float]):
        self._range = range_ms
        self.seek(0)

    def refresh(self):
        """Cambió algo solo visual (título, bloom, fondo cinemático): volver a dibujar aunque esté en pausa."""
        if self.scene:
            if not self.playing:
                self._render()
            self._wake()

    def update_cinematic(self):
        """Cambió la configuración cinemática (intervalos, tratamiento o video): actualiza sin reiniciar springs."""
        if self.scene and self._project is not None:
            self.scene.set_cinematic(getattr(self._project, "cinematic", None))
            if not self.playing:
                self._render()
            self._wake()

    def wait_video(self, timeout: float = 5.0):
        """Espera a que la pre-carga asíncrona complete los bloques pedidos y redibuja el instante actual."""
        if self.scene and self.scene.cinematic_bg is not None:
            self.scene.cinematic_bg.wait_idle(timeout)
            self._render()

    def _on_video_frame_ready(self):
        if self.scene and not self.playing:
            self.scene.invalidate_probe()
            self._render()

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
        self._lag0 = None
        self.scene.engine.warmup(self._range[0] + offset_ms)
        self._render()
        self.position.emit(offset_ms, self.duration)
        self._wake()

    def set_playing(self, on: bool):
        if on == self.playing or (on and not self.scene):
            return
        self.playing = on
        self._lag0 = None
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
        if self.playing and self._lag0 is None:
            self._lag0 = (perf_counter(), e.t - steps * DT)
        if self.playing and e.t * 1000 >= self._range[1]:
            self.seek(0)  # el clip se repite
            return
        self._settle = SETTLE_MS if self.playing else self._settle - steps * DT * 1000
        self._render()
        self.position.emit(e.t * 1000 - self._range[0], self.duration)
        if not self.playing and (self._settle <= 0 or e.is_settled()):
            self._timer.stop()

    def toggle_debug(self):
        self._debug = not self._debug
        self.update()

    def _render(self):
        t0 = perf_counter()
        canvas = self._surface.getCanvas()
        self.scene.clip_start_ms = self._range[0]
        self.scene.render(canvas)
        self._frame = self.ctx.read_rgba(self._surface)
        self._frame_ms = (perf_counter() - t0) * 1000
        self._frame_avg += (self._frame_ms - self._frame_avg) * 0.1
        h, w = self._frame.shape[:2]
        self._image = QImage(self._frame.data, w, h, w * 4, QImage.Format_RGBA8888)
        self.update()

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    def shutdown(self):
        self._timer.stop()
        if self.scene is not None:
            self.scene.close()
            self.scene = None
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
        if self._debug and self.scene:
            self._paint_debug(p, box)

    def _debug_lines(self) -> list[str]:
        """Text of the F3 overlay. Lag = wall-clock minus simulated time since play began: the preview has no audio clock to drift
        from, so this is the only way the picture can fall behind (ticks dropped by MAX_STEPS)."""
        e = self.scene.engine
        item, word = e.items[e.active], -1
        for i, w in enumerate(item.words):
            if w.b <= e.t:
                word = i
        lag = f"{((perf_counter() - self._lag0[0]) - (e.t - self._lag0[1])) * 1000:+.1f} ms" if self.playing and self._lag0 else "n/a"
        return [f"t {e.t:8.3f} s   clip +{e.t * 1000 - self._range[0]:.0f} ms",
                f"item {e.active}/{len(e.items) - 1} ({item.kind})   word {word}",
                f"lag {lag}",
                f"frame {self._frame_ms:5.1f} ms   avg {self._frame_avg:5.1f} ms"]

    def _paint_debug(self, p: QPainter, box: QRectF):
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPixelSize(max(10, int(box.width() / 34)))
        p.setFont(font)
        lines = self._debug_lines()
        lh = p.fontMetrics().height()
        p.fillRect(QRectF(box.left(), box.top(), box.width(), lh * len(lines) + 8), QColor(0, 0, 0, 170))
        p.setPen(QColor(120, 255, 160))
        for i, text in enumerate(lines):
            p.drawText(int(box.left()) + 6, int(box.top()) + 4 + lh * (i + 1) - p.fontMetrics().descent(), text)
