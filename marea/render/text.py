"""Fuentes y layout de texto de las líneas (CSS del prototipo expresado en em)."""
from functools import lru_cache
from pathlib import Path

import skia

FONTS = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"

LINE_H = 1.25  # line-height
PAD_V = 0.4  # padding vertical de cada línea (em)
PAD_L = 1.0  # margen del lado de la voz (em)
PAD_R = 0.15  # margen del lado contrario (fracción del ancho de la línea)
BG_SCALE = 0.7  # tamaño de las voces de fondo
BG_PAD_TOP = 0.3  # em de la voz de fondo
LETTER_SPACING = -0.012  # em


class Fonts:
    def __init__(self, family: str = ""):
        """family vacío = Inter Tight incluida; si no, una fuente instalada (cae a la incluida si no se encuentra)."""
        mgr = skia.FontMgr()
        self.bold = (family and mgr.matchFamilyStyle(family, skia.FontStyle.Bold())
                     or mgr.makeFromFile(str(FONTS / "InterTight-Bold.ttf")))
        self.regular = (family and mgr.matchFamilyStyle(family, skia.FontStyle.Normal())
                        or mgr.makeFromFile(str(FONTS / "InterTight-Regular.ttf")))

    def font(self, size: float, bold: bool = True) -> skia.Font:
        f = skia.Font(self.bold if bold else self.regular, size)
        f.setSubpixel(True)
        f.setLinearMetrics(True)
        f.setEdging(skia.Font.Edging.kAntiAlias)
        return f

    @lru_cache(maxsize=None)
    def text(self, s: str, size: float, bold: bool = True) -> tuple[skia.TextBlob, float]:
        """(blob con letter-spacing aplicado, ancho) de un texto sin saltos de línea."""
        font = self.font(size, bold)
        glyphs = font.textToGlyphs(s)
        ls, xs, x = LETTER_SPACING * size, [], 0.0
        for w in font.getWidths(glyphs):
            xs.append(x)
            x += w + ls
        return skia.TextBlob.MakeFromPosTextH(s, xs, 0, font), x

    @lru_cache(maxsize=None)
    def baseline(self, size: float) -> float:
        """Distancia del borde superior de la caja de línea (line-height 1.25) a la línea base."""
        m = self.font(size).getMetrics()
        return (LINE_H * size - (m.fDescent - m.fAscent)) / 2 - m.fAscent


def layout_block(words, fonts: Fonts, size: float, avail: float):
    """Salto de línea voraz entre palabras -> (filas [(palabra, x, ancho)], ancho de cada fila)."""
    space = fonts.text(" ", size)[1]
    rows, widths = [[]], [0.0]
    for w in words:
        ww = sum(fonts.text(p.text, size)[1] for p in w.parts)
        if rows[-1] and widths[-1] + space + ww > avail:
            rows.append([])
            widths.append(0.0)
        x = widths[-1] + (space if rows[-1] else 0.0)
        rows[-1].append((w, x, ww))
        widths[-1] = x + ww
    return rows, widths


CREDIT_SCALE = 0.42  # tamaño del crédito respecto a la letra
CREDIT_GAP_TOP = 1.1  # em de la letra entre el último verso y el crédito


def layout_credit(lines: list[tuple[str, str]], fonts: Fonts, size: float, avail: float):
    """Crédito bajo la letra («Written by A, B» / «Provided by Apple Music») ajustado a `avail` px.
    -> (filas [[(palabra, x, negrita)]] por cada línea de crédito, tamaño de letra, alto total en px)."""
    csize = CREDIT_SCALE * size
    space = fonts.text(" ", csize, False)[1]
    blocks = []
    for label, text in lines:
        rows, x = [[]], 0.0
        for word, bold in [(w, False) for w in label.split()] + [(w, True) for w in text.split()]:
            w = fonts.text(word, csize, bold)[1]
            if rows[-1] and x + space + w > avail:
                rows.append([])
                x = 0.0
            x += space if rows[-1] else 0.0
            rows[-1].append((word, x, bold))
            x += w
        blocks.append(rows)
    n_rows = sum(len(r) for r in blocks)
    height = CREDIT_GAP_TOP * size + n_rows * LINE_H * csize + (len(blocks) - 1) * 0.35 * csize + PAD_V * size if blocks else 0.0
    return blocks, csize, height


class ItemLayout:
    """Geometría de una línea para un ancho y un tamaño de letra dados."""

    def __init__(self, item, fonts: Fonts, size: float, width: float):
        self.size, self.width = size, width
        self.pad_l = PAD_L * size if not item.duet else PAD_R * width
        self.pad_r = PAD_R * width if not item.duet else PAD_L * size
        avail = width - self.pad_l - self.pad_r
        self.bg_size = BG_SCALE * size
        self.main, self.main_w = layout_block([w for w in item.words if not w.bg], fonts, size, avail)
        bg = [w for w in item.words if w.bg]
        self.bg, self.bg_w = layout_block(bg, fonts, self.bg_size, avail) if bg else ([], [])
        self.main_h = len(self.main) * LINE_H * size
        self.bg_h = (BG_PAD_TOP + len(self.bg) * LINE_H) * self.bg_size if bg else 0.0
        self.h_inactive = 2 * PAD_V * size + self.main_h
        self.h_active = self.h_inactive + self.bg_h
