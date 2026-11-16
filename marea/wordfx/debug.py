"""Superposición de depuración (solo preview, nunca en el export): qué se detectó en cada palabra con efecto."""
import skia

from marea.wordfx.fxtypes import AXES, FxWord

LABEL_SCALE = 0.3  # tamaño de la etiqueta respecto a la letra
AMBER = (255, 197, 107)


def label_lines(fx: FxWord) -> list[str]:
    """Lema · idioma · categorías / vector final / contagio y primitivos activos."""
    m, p = fx.match, fx.params
    vec = " ".join(f"{a[:3]} {v:+.2f}" for a, v in zip(AXES, p.vector))
    bleed = f"contagio {p.bleed:.2f} ({p.bleed_from})" if p.bleed else "sin contagio"
    rep = f" · rep {p.repeat_n}" if p.repeat_n else ""
    note = f" · {m.note}" if m.note else ""
    var = f" · {p.variant}" if p.variant else ""
    return [f"[{m.line}.{m.word}] {m.lemma} · {m.lang} · {'+'.join(m.categories)}{var}{rep}{note}", vec,
            f"{bleed} · {', '.join(p.primitives)}"]


def draw_label(canvas: skia.Canvas, fonts, fx: FxWord, x: float, ww: float, base: float, size: float):
    """Etiqueta sobre la palabra (x = borde izquierdo, ww = ancho, base = línea base) y una raya fina bajo ella."""
    paint = skia.Paint(AntiAlias=True, Color=skia.ColorSetARGB(230, *AMBER))
    lines = label_lines(fx)
    for i, text in enumerate(lines):
        blob, tw = fonts.text(text, LABEL_SCALE * size, False)
        canvas.drawTextBlob(blob, x + (ww - tw) / 2, base - (1.0 + 0.36 * (len(lines) - 1 - i)) * size, paint)
    paint.setStrokeWidth(max(1.0, 0.04 * size))
    canvas.drawLine(x, base + 0.12 * size, x + ww, base + 0.12 * size, paint)
