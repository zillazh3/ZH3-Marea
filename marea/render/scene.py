"""Escena de un clip: dibuja el estado del motor en un canvas de skia.

`render_frame(scene, canvas)` es lo único que llaman la preview y el export: el motor (marea.engine) avanza
el estado, aquí solo se dibuja. Todo se mide en px del lienzo; blur y glow vienen del motor en px de diseño
(letra de 42 px) y se escalan con `k = tamaño de letra / 42`.
"""
import math
from dataclasses import dataclass

import skia

from marea.engine.engine import Engine, Params
from marea.render.background import Background
from marea.render.text import (BG_PAD_TOP, CREDIT_GAP_TOP, LINE_H, PAD_L, PAD_R, PAD_V, Fonts, ItemLayout, layout_credit)

DESIGN_PX = 42.0
FW = 0.5  # borde del wipe (em)
FW_SOFT = 1.3  # borde del wipe en letra por línea (em): degradado ancho, como Spicy Lyrics sin sílabas
ORIGIN_SYL, ORIGIN_CH = 0.81, 0.75  # transform-origin vertical (em desde el borde superior de la línea)
BLOOM = ((0.28, 0.16), (0.06, 0.22))  # (radio em, alpha) de las dos sombras
WHITE = skia.ColorSetARGB(255, 255, 255, 255)


@dataclass
class Style:
    font_frac: float = 0.066  # tamaño de letra / ancho del lienzo
    bloom: bool = True
    title: str = ""
    artist: str = ""
    font_family: str = ""  # vacío = Inter Tight incluida
    wm_text: str = "Zilla-ZH3"  # marca de agua; vacío = sin marca
    wm_side: str = "left"  # "left" | "right"
    wm_glyph: bool = False  # tres ondas junto al texto
    credit: bool = True  # «Written by… / Provided by…» al final de la letra, como en Spicetify


WM_SIZE = 0.022  # alto de la letra / alto del lienzo
WM_ALPHA, WM_FADE_MS = 0.4, 600
WM_BASELINE = 0.835  # por encima del 15 % inferior que tapa la interfaz de TikTok
WM_RIGHT_MARGIN = 0.17  # fracción de `ref` que se deja libre a la derecha (columna de botones de TikTok)
WM_DODGE = 0.7  # cuánto baja la opacidad al cruzarse con la línea activa


def _argb(a: float) -> int:
    return skia.ColorSetARGB(round(max(0.0, min(1.0, a)) * 255), 255, 255, 255)


def _paint(**kw) -> skia.Paint:
    p = skia.Paint(AntiAlias=True, Color=WHITE)
    for k, v in kw.items():
        if v is not None:
            getattr(p, "set" + k)(v)
    return p


class Scene:
    def __init__(self, lyrics, cover: skia.Image, size=(1080, 1920), params: Params | None = None,
                 style: Style | None = None, gpu: bool = False):
        """gpu=True si el canvas es de una superficie GPU (`marea.render.gpu`); la portada debe estar en ese contexto."""
        self.w, self.h = size
        self.style = style or Style()
        self.cover = cover
        self.fonts = Fonts(self.style.font_family)
        self.bg = Background(cover, self.w, self.h, lowres=1 if gpu else 4)
        # medidas relativas a `ref` (el ancho en 9:16): sirven igual para 1080x1080 y 1920x1080
        self.ref = min(self.w, 0.8 * self.h)
        self.size = self.style.font_frac * self.ref
        self.k = self.size / DESIGN_PX
        stage_w = min(0.94 * self.w, 1.2 * self.h)  # en horizontal, la letra queda en una columna centrada
        self.stage = skia.Rect.MakeXYWH((self.w - stage_w) / 2, 0.16 * self.h, stage_w, 0.72 * self.h)
        credit = lyrics.credit_lines() if self.style.credit else []
        self._credit_rows, self._credit_size, credit_h = layout_credit(
            credit, self.fonts, self.size, stage_w - PAD_L * self.size - PAD_R * stage_w)
        self.engine = Engine(lyrics, self._measure, self.stage.height(), self.size, params, credit_h=credit_h)
        self._by_line = {id(L.src): L for L in self.engine.items if not (L.is_gap or L.is_credit)}
        self._layouts = {}
        self.clip_start_ms: float | None = None  # inicio del clip: la marca de agua entra con fundido desde ahí

    # ---- layout ----
    def _layout(self, item) -> ItemLayout:
        lay = self._layouts.get(id(item))
        if lay is None:
            lay = self._layouts[id(item)] = ItemLayout(item, self.fonts, self.size, self.stage.width())
        return lay

    def _measure(self, line, active):
        lay = self._layout(self._by_line[id(line)])
        return lay.h_active if active else lay.h_inactive

    # ---- dibujo ----
    def render(self, canvas: skia.Canvas):
        e, st = self.engine, self.stage
        self.bg.draw(canvas, e.t)
        self._header(canvas)
        canvas.save()
        canvas.clipRect(st)
        canvas.saveLayer(st, None)
        canvas.save()
        canvas.translate(st.left(), st.top())
        for L in e.items:
            self._item(canvas, L)
        canvas.restore()
        fade = skia.GradientShader.MakeLinear([(0, st.top()), (0, st.bottom())],  # máscara de bordes del prototipo
                                              [_argb(0), _argb(1), _argb(1), _argb(0)], [0, 0.06, 0.90, 1])
        canvas.drawRect(st, skia.Paint(Shader=fade, BlendMode=skia.BlendMode.kDstIn))
        canvas.restore()
        canvas.restore()
        self._watermark(canvas)

    def _watermark_box(self, sz: float) -> skia.Rect | None:
        st = self.style
        if not st.wm_text.strip():
            return None
        gw = 1.5 * sz if st.wm_glyph else 0.0
        w = gw + (0.5 * sz if gw else 0.0) + self.fonts.text(st.wm_text, sz)[1]
        x = self.w - WM_RIGHT_MARGIN * self.ref - w if st.wm_side == "right" else self.stage.left() + 0.04 * self.ref
        base = WM_BASELINE * self.h
        return skia.Rect.MakeXYWH(x, base - sz, w, 1.25 * sz)

    def _wm_dodge(self, box: skia.Rect, sz: float) -> float:
        """0..1: cuánto se cruza la línea cantada con la marca (función continua del estado del motor, sin memoria)."""
        worst, m = 0.0, 0.5 * sz
        for L in self.engine.items:
            if L.is_gap or L.act < 0.05:
                continue
            lay = self._layout(L)
            top = self.stage.top() + L.y.p
            v = (min(top + L.h, box.bottom() + m) - max(top, box.top() - m)) / (box.height() + 2 * m)
            tw = max(lay.main_w)
            x1 = self.stage.left() + (lay.width - lay.pad_r if L.duet else lay.pad_l + tw)
            hx = (min(x1, box.right() + m) - max(x1 - tw, box.left() - m)) / (0.5 * box.width())
            worst = max(worst, L.act * min(1.0, max(0.0, v)) * min(1.0, max(0.0, hx)))
        return worst

    def _watermark(self, canvas):
        sz = WM_SIZE * self.h
        box = self._watermark_box(sz)
        if box is None:
            return
        fade = 1.0 if self.clip_start_ms is None else (self.engine.t * 1000 - self.clip_start_ms) / WM_FADE_MS
        alpha = WM_ALPHA * min(1.0, max(0.0, fade)) * (1 - WM_DODGE * self._wm_dodge(box, sz))
        if alpha < 0.003:
            return
        blob, tw = self.fonts.text(self.style.wm_text, sz)
        tx = box.left() + (box.width() - tw)
        base = box.top() + sz  # línea base
        shadow = skia.Paint(AntiAlias=True, Color=skia.ColorSetARGB(round(alpha * 0.9 * 255), 0, 0, 0),
                            MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 0.25 * sz))
        canvas.drawTextBlob(blob, tx, base + 0.06 * sz, shadow)
        canvas.drawTextBlob(blob, tx, base, _paint(Color=_argb(alpha)))
        if self.style.wm_glyph:
            self._wm_waves(canvas, box.left(), base - 0.35 * sz, 1.5 * sz, sz, alpha)

    def _wm_waves(self, canvas, x, cy, w, sz, alpha):
        """Tres ondas apiladas (la central más gruesa), como el logo."""
        for i, (dy, thick) in enumerate(((-0.34, 0.07), (0.0, 0.12), (0.34, 0.07))):
            path = skia.Path()
            for s in range(17):
                px = x + w * s / 16
                py = cy + dy * sz + 0.1 * sz * math.sin((s / 16 * 2 + i * 0.3) * math.pi)
                path.moveTo(px, py) if s == 0 else path.lineTo(px, py)
            canvas.drawPath(path, _paint(Color=_argb(alpha), Style=skia.Paint.kStroke_Style, StrokeWidth=thick * sz,
                                         StrokeCap=skia.Paint.kRound_Cap, StrokeJoin=skia.Paint.kRound_Join))

    def snapshot(self, t_ms: float) -> skia.Image:
        """Frame estático en t_ms con el motor ya asentado (3 s de pre-roll). Úsese con una escena nueva."""
        self.engine.warmup(t_ms)
        s = skia.Surface(self.w, self.h)
        self.render(s.getCanvas())
        return s.makeImageSnapshot()

    def _header(self, canvas):
        H, R, st = self.h, self.ref, self.style
        cs, x, y = 0.14 * R, self.stage.left() + 0.04 * R, 0.06 * H
        box = skia.Rect.MakeXYWH(x, y, cs, cs)
        canvas.save()
        canvas.clipRRect(skia.RRect.MakeRectXY(box, cs * 0.14, cs * 0.14), doAntiAlias=True)
        canvas.drawImageRect(self.cover, box, skia.SamplingOptions(skia.FilterMode.kLinear))
        canvas.restore()
        tx = x + cs + 0.035 * R
        avail = self.stage.right() - 0.04 * R - tx
        for text, size, base, alpha, bold in ((st.title, 0.04 * R, 0.45, 1.0, True), (st.artist, 0.032 * R, 0.80, 0.62, False)):
            if text:
                font = self.fonts.font(size, bold)
                while font.measureText(text) > avail and len(text) > 1:
                    text = text[:-2] + "…"
                canvas.drawString(text, tx, y + cs * base, font, _paint(Alphaf=alpha))

    def _item(self, canvas, L):
        st_w, k = self.stage.width(), self.k
        if L.is_gap and L.grp_op < 0.001:
            return
        sigma = L.blur * k
        m = 0.6 * self.size + 3 * sigma
        if L.y.p + L.h < -m or L.y.p > self.stage.height() + m:
            return
        canvas.save()
        canvas.translate(0, L.y.p)
        ox = st_w if L.duet and not L.is_gap else 0.0  # transform-origin: right/left center
        canvas.translate(ox, L.h / 2)
        canvas.scale(L.s.p, L.s.p)
        canvas.translate(-ox, -L.h / 2)
        layer = L.opacity < 0.999 or sigma > 0.01
        if layer:
            canvas.saveLayer(skia.Rect.MakeLTRB(-m, -m, st_w + m, L.h + m), _paint(
                Alphaf=L.opacity, ImageFilter=skia.ImageFilters.Blur(sigma, sigma) if sigma > 0.01 else None))
        if L.is_gap:
            self._gap(canvas, L)
        elif L.is_credit:
            self._credit(canvas, L)
        else:
            self._line(canvas, L, self._layout(L))
        if layer:
            canvas.restore()
        canvas.restore()

    def _credit(self, canvas, L):
        """«Written by A, B» y «Provided by Apple Music»: el prefijo tenue y el nombre más claro, en pequeño."""
        csize, size = self._credit_size, self.size
        y = CREDIT_GAP_TOP * size
        for rows in self._credit_rows:
            for row in rows:
                base = y + self.fonts.baseline(csize)
                for word, x, bold in row:
                    blob, _ = self.fonts.text(word, csize, bold)
                    canvas.drawTextBlob(blob, PAD_L * size + x, base, _paint(Color=_argb(0.62 if bold else 0.38)))
                y += LINE_H * csize
            y += 0.35 * csize

    def _gap(self, canvas, L):
        size, w = self.size, self.stage.width()
        d, gap = 0.32 * size, 0.24 * size
        gw = 3 * d + 2 * gap
        left = w - 0.15 * w - gw if L.duet else size  # con dueto siguiente, a la derecha
        cy = L.h / 2
        canvas.save()
        canvas.translate(left + gw / 2, cy)
        canvas.scale(L.grp_sc, L.grp_sc)
        canvas.translate(-gw / 2, 0)
        for i, dot in enumerate(L.dots):
            canvas.save()
            canvas.translate(i * (d + gap) + d / 2, dot.y * size)
            canvas.scale(dot.scale, dot.scale)
            if dot.halo_a > 0:
                sigma = dot.halo_r * size / 2
                canvas.drawCircle(0, 0, d / 2, _paint(
                    Color=_argb(dot.halo_a * dot.op * L.grp_op), MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma)))
            canvas.drawCircle(0, 0, d / 2, _paint(Color=_argb(dot.op * L.grp_op)))
            canvas.restore()
        canvas.restore()

    def _line(self, canvas, L, lay):
        size, ba, da = lay.size, 0.2 + 0.8 * L.act, 0.2 + 0.2 * L.act
        bloom = None
        if L.on and self.style.bloom:  # dos drop-shadow encadenados sobre lo ya cantado
            f = None
            for r, a in BLOOM:
                f = skia.ImageFilters.DropShadow(0, 0, r * size / 2, r * size / 2, _argb(a), f)
            bloom = f
        m = BLOOM[0][0] * size * 2
        top = PAD_V * size
        if bloom:
            canvas.saveLayer(skia.Rect.MakeLTRB(-m, -m, lay.width + m, L.h + m), _paint(ImageFilter=bloom))
        self._block(canvas, L, lay, lay.main, lay.main_w, size, top, ba, da)
        if bloom:
            canvas.restore()
        if lay.bg and L.bg_op > 0.003:
            box_top = top + lay.main_h
            ox = lay.width - lay.pad_r if L.duet else lay.pad_l
            canvas.save()
            canvas.translate(ox, box_top - 0.8 * lay.bg_h * (1 - L.bg_p))  # entra deslizándose desde -80 %
            canvas.scale(0.8 + 0.2 * L.bg_p, 0.8 + 0.2 * L.bg_p)
            canvas.translate(-ox, -box_top)
            canvas.saveLayer(skia.Rect.MakeLTRB(-m, box_top - m, lay.width + m, box_top + lay.bg_h + m),
                             _paint(Alphaf=0.4 * L.bg_op, ImageFilter=bloom))
            self._block(canvas, L, lay, lay.bg, lay.bg_w, lay.bg_size, box_top + BG_PAD_TOP * lay.bg_size, ba, da)
            canvas.restore()
            canvas.restore()

    def _block(self, canvas, L, lay, rows, widths, size, top, ba, da):
        for ri, (row, rw) in enumerate(zip(rows, widths)):
            x0 = lay.width - lay.pad_r - rw if L.duet else lay.pad_l  # dueto: alineado a la derecha
            for word, wx, ww in row:
                self._word(canvas, word, x0 + wx, top + ri * LINE_H * size, ww, size, ba, da)

    def _wipe(self, x, w, p, ba, da, size, a=1.0, fw=FW):
        """Máscara del prototipo: gradiente horizontal de alpha ba→da con borde de `fw` em que avanza con p."""
        x1 = x + p * (w + fw * size) - fw * size
        return skia.GradientShader.MakeLinear([(x1, 0), (x1 + fw * size, 0)], [_argb(a * ba), _argb(a * da)])

    def _word(self, canvas, word, x, line_top, ww, size, ba, da):
        base = line_top + self.fonts.baseline(size)
        if not word.emph:
            fw = FW_SOFT if word.soft else FW
            for u in word.parts:
                blob, pw = self.fonts.text(u.text, size)
                text = skia.Paint(AntiAlias=True, Shader=self._wipe(x, pw, u.p, ba, da, size, fw=fw))
                glow = (skia.Paint(AntiAlias=True, Shader=self._wipe(x, pw, u.p, ba, da, size, u.glow_a, fw),
                                   MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, u.glow_r * self.k / 2))
                        if u.glow_a > 0 else None)
                self._glyphs(canvas, u, blob, x, base, pw, size, line_top + ORIGIN_SYL * size, text, glow)
                x += pw
            return
        # palabra con énfasis: una sola máscara para la palabra, las letras se animan dentro
        pad = 0.6 * size
        bounds = skia.Rect.MakeLTRB(x - pad, line_top - pad, x + ww + pad, line_top + LINE_H * size + pad)
        canvas.saveLayer(bounds, None)
        cx, filled = x, 0.0  # filled: px de la palabra ya rellenos; cada letra aporta su ancho real, no una fracción igual
        for c in word.parts:
            blob, pw = self.fonts.text(c.text, size)
            glow = (skia.Paint(AntiAlias=True, Color=_argb(c.glow_a),
                               MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, c.glow_r * self.k / 2))
                    if c.glow_a > 0 else None)
            self._glyphs(canvas, c, blob, cx, base, pw, size, line_top + ORIGIN_CH * size,
                         skia.Paint(AntiAlias=True, Color=WHITE), glow)
            cx += pw
            filled += pw * c.p
        canvas.drawRect(bounds, skia.Paint(Shader=self._wipe(x, ww, filled / ww if ww else 0.0, ba, da, size),
                                           BlendMode=skia.BlendMode.kDstIn))
        canvas.restore()

    def _glyphs(self, canvas, u, blob, x, base, pw, size, oy, text, glow):
        ox = x + pw / 2
        canvas.save()
        canvas.translate(ox + u.x * size, oy + u.y * size)
        canvas.scale(u.scale, u.scale)
        canvas.translate(-ox, -oy)
        if u.wdy is None:
            if glow:
                canvas.drawTextBlob(blob, x, base, glow)
            canvas.drawTextBlob(blob, x, base, text)
        else:  # capa por carácter (motion): cada letra sube / crece sobre su propio centro, dentro del movimiento de la sílaba
            cx = x  # ponytail: se parte por code point; marcas combinantes se separarían de su base (agrupar si aparece)
            for ch, dy, ds in zip(u.text, u.wdy, u.wds):
                cblob, cw = self.fonts.text(ch, size)  # sin kerning: los anchos suman lo mismo que el bloque de la sílaba
                canvas.save()
                canvas.translate(cx + cw / 2, oy + dy * size)
                canvas.scale(1 + ds, 1 + ds)
                canvas.translate(-(cx + cw / 2), -oy)
                if glow:
                    canvas.drawTextBlob(cblob, cx, base, glow)
                canvas.drawTextBlob(cblob, cx, base, text)
                canvas.restore()
                cx += cw
        canvas.restore()


def render_frame(scene: Scene, canvas: skia.Canvas):
    scene.render(canvas)
