"""Portada sintética de la demo: la del prototipo (paleta violeta) dibujada con skia."""
import math
from pathlib import Path

import skia

FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "InterTight-Bold.ttf"
COLORS = ["#2b34e0", "#6a2cff", "#b39cff", "#14105e", "#3f83ff"]


def demo_cover(size: int = 600, label: bool = True) -> skia.Image:
    """label=False: solo el mesh de colores, sin el texto «MAREA DE LUZ» (portada de relleno)."""
    s = skia.Surface(size, size)
    c = s.getCanvas()
    c.clear(skia.ColorSetRGB(0x0D, 0x0B, 0x26))
    for i, col in enumerate(COLORS):  # mesh del prototipo en t = 12 s
        sp, ph, r = 0.05 + i * 0.013, i * 1.7, 0.55 + (i % 3) * 0.15
        x = size * (0.5 + 0.38 * math.sin(12 * sp + ph))
        y = size * (0.5 + 0.38 * math.cos(12 * sp * 1.3 + ph * 0.7))
        rgb = int(col[1:], 16)
        grad = skia.GradientShader.MakeRadial((x, y), size * r, [skia.ColorSetARGB(255, *rgb.to_bytes(3, "big")),
                                                                  skia.ColorSetARGB(0, *rgb.to_bytes(3, "big"))])
        c.drawRect(skia.Rect.MakeWH(size, size), skia.Paint(Shader=grad))
    blurred = skia.Paint(ImageFilter=skia.ImageFilters.Blur(size * 0.05, size * 0.05, skia.TileMode.kClamp))
    c.drawImage(s.makeImageSnapshot(), 0, 0, skia.SamplingOptions(), blurred)  # blur de 30 px a 600
    if not label:
        return s.makeImageSnapshot()
    font = skia.Font(skia.FontMgr().makeFromFile(str(FONT)), size * 0.097)
    white = skia.Paint(AntiAlias=True, Color=skia.ColorSetARGB(235, 255, 255, 255))
    c.drawString("MAREA", size * 0.067, size * 0.8, font, white)
    c.drawString("DE LUZ", size * 0.067, size * 0.9, font, white)
    return s.makeImageSnapshot()
