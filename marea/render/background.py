"""Fondo estilo Apple Music: capas de la portada muy difuminadas, rotando y escalándose lentamente.

Lo pesado (recorte, saturación ×1.3, blur gaussiano) se calcula una vez a baja resolución; por frame solo se
transforma y compone cada capa. Todo depende solo de `t`, así que es determinista.
"""
import math

import skia

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
    def __init__(self, cover: skia.Image, w: int, h: int, lowres: int = LOWRES):
        """lowres=1 compone directo en el lienzo (GPU); en CPU conviene 4."""
        self.full = (w, h)
        w, h = max(1, w // lowres), max(1, h // lowres)
        self.surface = skia.Surface(w, h) if lowres > 1 else None
        self.w, self.h = w, h
        diag = math.hypot(w, h)
        self.drift = DRIFT * diag
        self.size = (diag + 2 * self.drift) / (1 - SCALE_SWING)  # cubre el lienzo con cualquier giro y deriva
        sigma = BLUR * w * RES / self.size  # el blur es 8 % del ancho en el lienzo
        cw, ch = cover.width(), cover.height()
        side = min(cw, ch)
        self.layers = []
        for *_, zoom in LAYERS:
            s = skia.Surface(RES, RES)
            c = s.getCanvas()
            src_side = side / zoom
            src = skia.Rect.MakeXYWH((cw - src_side) / 2, (ch - src_side) / 2, src_side, src_side)
            paint = skia.Paint(ColorFilter=_saturation(SAT),
                               ImageFilter=skia.ImageFilters.Blur(sigma, sigma, skia.TileMode.kMirror))
            c.drawImageRect(cover, src, skia.Rect.MakeWH(RES, RES), skia.SamplingOptions(skia.FilterMode.kLinear),
                            paint, skia.Canvas.kFast_SrcRectConstraint)
            self.layers.append(s.makeImageSnapshot())

    def draw(self, canvas: skia.Canvas, t: float):
        if self.surface is None:
            return self._compose(canvas, t)
        self._compose(self.surface.getCanvas(), t)
        canvas.drawImageRect(self.surface.makeImageSnapshot(), skia.Rect.MakeWH(*self.full),
                             skia.SamplingOptions(skia.FilterMode.kLinear))

    def _compose(self, canvas: skia.Canvas, t: float):
        half, sampling = self.size / 2, skia.SamplingOptions(skia.FilterMode.kLinear)
        for img, (period, direction, alpha, phase, _) in zip(self.layers, LAYERS):
            a = 2 * math.pi * t / period + phase
            canvas.save()
            canvas.translate(self.w / 2 + self.drift * math.sin(a), self.h / 2 + self.drift * math.cos(a * 0.8))
            canvas.rotate(direction * 360 * t / (3 * period))
            canvas.scale(*(1 + SCALE_SWING * math.sin(a * 1.3 + phase),) * 2)
            canvas.drawImageRect(img, skia.Rect.MakeXYWH(-half, -half, self.size, self.size), sampling,
                                 skia.Paint(Alphaf=alpha))
            canvas.restore()
        canvas.drawColor(skia.ColorSetARGB(round(VEIL * 255), 0, 0, 0))
