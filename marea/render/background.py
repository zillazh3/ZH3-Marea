"""Fondo estilo Apple Music: capas de la portada muy difuminadas, rotando y escalándose lentamente.

Lo pesado (recorte, saturación ×1.3, blur gaussiano) se calcula una vez a baja resolución; por frame solo se
transforma y compone cada capa. Todo depende solo de `t`, así que es determinista.
"""
import math

import skia

from marea.model.loop import LoopSpec

# (periodo s, sentido, alpha, fase, zoom sobre la portada) — ciclos de 24 a 40 s
LAYERS = [(36, 1, 1.0, 0.0, 1.0), (28, -1, 0.55, 2.1, 1.7), (40, 1, 0.5, 4.2, 1.3), (24, -1, 0.4, 5.3, 2.2)]
RES = 192  # lado de cada capa precalculada
SAT = 1.3
VEIL = 0.22
BLUR = 0.08  # fracción del ancho del lienzo
DRIFT = 0.10  # desplazamiento máximo del centro (fracción de la diagonal)
SCALE_SWING = 0.12


def _saturation(s):
    r, g, b = 0.2126, 0.7152, 0.0722  # luma
    i = 1 - s
    return skia.ColorFilters.Matrix([
        r * i + s, g * i, b * i, 0, 0,
        r * i, g * i + s, b * i, 0, 0,
        r * i, g * i, b * i + s, 0, 0,
        0, 0, 0, 1, 0])


LOWRES = 4  # en CPU el fondo (puro blur) se compone a 1/4 de resolución y se escala una vez al lienzo


class Background:
    def __init__(self, cover: skia.Image, w: int, h: int, lowres: int = LOWRES, blur: float = 1.0, veil: float = VEIL,
                 sat: float = SAT, speed: float = 1.0):
        """lowres=1 compone directo en el lienzo (GPU); en CPU conviene 4. blur/veil/sat/speed: multiplicador del desenfoque,
        oscuridad del velo (0..1), saturación y velocidad del movimiento; los valores por omisión son el look de siempre."""
        self.full = (w, h)
        self.veil, self.speed = veil, speed
        w, h = max(1, w // lowres), max(1, h // lowres)
        self.surface = skia.Surface(w, h) if lowres > 1 else None
        self.w, self.h = w, h
        diag = math.hypot(w, h)
        self.drift = DRIFT * diag
        self.size = (diag + 2 * self.drift) / (1 - SCALE_SWING)  # cubre el lienzo con cualquier giro y deriva
        sigma = BLUR * blur * w * RES / self.size  # el blur es 8 % del ancho en el lienzo (× el multiplicador)
        cw, ch = cover.width(), cover.height()
        side = min(cw, ch)
        self.layers = []
        self._sampling = skia.SamplingOptions(skia.FilterMode.kLinear)
        self._full_rect = skia.Rect.MakeWH(*self.full)
        self._layer_rect = skia.Rect.MakeXYWH(-self.size / 2, -self.size / 2, self.size, self.size)
        self._layer_paints = [skia.Paint(Alphaf=alpha) for _, _, alpha, _, _ in LAYERS]
        for *_, zoom in LAYERS:
            s = skia.Surface(RES, RES)
            c = s.getCanvas()
            src_side = side / zoom
            src = skia.Rect.MakeXYWH((cw - src_side) / 2, (ch - src_side) / 2, src_side, src_side)
            paint = skia.Paint(ColorFilter=_saturation(sat),
                               ImageFilter=skia.ImageFilters.Blur(sigma, sigma, skia.TileMode.kMirror))
            c.drawImageRect(cover, src, skia.Rect.MakeWH(RES, RES), self._sampling,
                            paint, skia.Canvas.kFast_SrcRectConstraint)
            self.layers.append(s.makeImageSnapshot())

    def draw(self, canvas: skia.Canvas, t: float):
        if self.surface is None:
            return self._compose(canvas, t)
        self._compose(self.surface.getCanvas(), t)
        canvas.drawImageRect(self.surface.makeImageSnapshot(), self._full_rect, self._sampling)

    def draw_loop(self, canvas: skia.Canvas, phase_ms: float, spec: LoopSpec):
        """Keep every motion amplitude and initial phase, with whole cycles per clip."""
        target = self.surface.getCanvas() if self.surface is not None else canvas
        duration_s = spec.duration_ms / 1000.0
        phase = (phase_ms % spec.duration_ms) / spec.duration_ms
        start_s = spec.start_ms / 1000.0 * self.speed

        def angle(period, factor, offset=0.0):
            cycles = max(1, int(math.floor(abs(self.speed) * duration_s * factor / period + 0.5)))
            winding = math.copysign(cycles, self.speed) if self.speed else 0.0
            return math.tau * (factor * start_s / period + winding * phase) + offset

        for img, paint, (period, direction, _, initial, _) in zip(self.layers, self._layer_paints, LAYERS):
            target.save()
            target.translate(self.w / 2 + self.drift * math.sin(angle(period, 1.0, initial)),
                             self.h / 2 + self.drift * math.cos(angle(period, 0.8, initial * 0.8)))
            target.rotate(direction * math.degrees(angle(period, 1.0 / 3.0)))
            target.scale(*(1 + SCALE_SWING * math.sin(angle(period, 1.3, initial * 2.3)),) * 2)
            target.drawImageRect(img, self._layer_rect, self._sampling, paint)
            target.restore()
        target.drawColor(skia.ColorSetARGB(round(self.veil * 255), 0, 0, 0))
        if self.surface is not None:
            canvas.drawImageRect(self.surface.makeImageSnapshot(), self._full_rect, self._sampling)

    def _compose(self, canvas: skia.Canvas, t: float):
        t = t * self.speed
        for img, paint, (period, direction, _, phase, _) in zip(self.layers, self._layer_paints, LAYERS):
            a = 2 * math.pi * t / period + phase
            canvas.save()
            canvas.translate(self.w / 2 + self.drift * math.sin(a), self.h / 2 + self.drift * math.cos(a * 0.8))
            canvas.rotate(direction * 360 * t / (3 * period))
            canvas.scale(*(1 + SCALE_SWING * math.sin(a * 1.3 + phase),) * 2)
            canvas.drawImageRect(img, self._layer_rect, self._sampling, paint)
            canvas.restore()
        canvas.drawColor(skia.ColorSetARGB(round(self.veil * 255), 0, 0, 0))
