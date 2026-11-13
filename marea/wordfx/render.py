"""Dibujo skia de los efectos por palabra. La escena abre la capa y aplica después su máscara de cantado (kDstIn):
aquí se dibujan las letras en su color final, sin fondo ni máscara. Todo es función de (t, FxParams, índice de letra)."""
import math
from dataclasses import dataclass, replace
from functools import lru_cache

import skia

from marea.wordfx.anim import LetterState, echoes, letter_state, particles, smoothstep, strain_boost
from marea.wordfx.contrast import adapt_params
from marea.wordfx.fxtypes import CarrierWord, FxWord

GHOSTS = ((0.12, 0.1), (0.08, 0.2), (0.04, 0.35))  # (retraso s, alpha base): del más viejo al más reciente
GLOW_ASC, GLOW_DESC = 1.0, 0.4  # em sobre/bajo la línea base que cubre la imagen del glow
GLOW_SIGMA = 0.125  # em; radio visual ~0.25 em
HALO_SIGMA = 0.035  # em; halo suave de cada mota
GRAIN_VARIANTS = 16
GRAIN_TILE = 128
CENTER_Y = 0.35  # em sobre la línea base: centro vertical en el que giran/escalan las letras
FRACTURE_GAP = 0.08  # em de separación final entre mitades
FRACTURE_ROT = 4.0  # grados por mitad a fracture=1
CHROMA_MIN_PX = 0.3
BLUR_MIN_PX = 0.4


@dataclass(frozen=True)
class Letter:
    ch: str
    x: float  # px desde el borde izquierdo de la palabra
    w: float  # avance en px


def letters_for(fonts, text: str, size: float) -> list[Letter]:
    """Geometría por carácter: anchos acumulados, sin kerning (misma convención que Scene._glyphs)."""
    out, x = [], 0.0
    for ch in text:
        w = fonts.text(ch, size)[1]
        out.append(Letter(ch, x, w))
        x += w
    return out


@lru_cache(maxsize=160)  # ~0.4 MB cada una a 60 px: 160 ≈ 64 MB como techo (una letra y tamaño por entrada)
def _glow_image(fonts, ch: str, size: float, w: float) -> tuple[skia.Image, int]:
    """Resplandor blanco de una letra, pre-desenfocado y cacheado (un blur por (letra, tamaño), no por fotograma).
    Se tiñe y se pone alpha al dibujarlo; memoización pura, el mismo (letra, tamaño) da siempre la misma imagen."""
    pad = math.ceil(3 * GLOW_SIGMA * size)
    surf = skia.Surface(math.ceil(w) + 2 * pad, math.ceil((GLOW_ASC + GLOW_DESC) * size) + 2 * pad)
    surf.getCanvas().drawTextBlob(fonts.text(ch, size)[0], pad, pad + GLOW_ASC * size, skia.Paint(
        AntiAlias=True, Color=skia.ColorWHITE, MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, GLOW_SIGMA * size)))
    return surf.makeImageSnapshot(), pad


def _argb(c, a: float) -> int:
    return skia.ColorSetARGB(round(min(1.0, max(0.0, a)) * 255), *c)


@lru_cache(maxsize=GRAIN_VARIANTS)
def _grain_tile(variant: int) -> skia.Image:
    """Tesela de ruido; se cachea por variante (memoización pura: mismo variant, misma imagen)."""
    surf = skia.Surface(GRAIN_TILE, GRAIN_TILE)
    surf.getCanvas().drawPaint(skia.Paint(
        Shader=skia.PerlinNoiseShader.MakeFractalNoise(0.9, 0.9, 1, float(variant * 7 + 1))))
    return surf.makeImageSnapshot()


def _body(canvas, blob, lx, base, size, w, color, fr, chroma, flip, h_slant):
    """Tinta de una letra: 1 o 2 mitades (fractura) x 1 o 3 canales (cromática). Dentro ya no hay alpha propio si hay capa."""
    if fr < 0.01:
        halves = (0,)
    else:
        halves = (-1, 1)
    cx, cy = lx + w / 2, base - CENTER_Y * size
    for h in halves:
        canvas.save()
        if h:
            canvas.translate(h * fr * FRACTURE_GAP * size / 2, 0)
            canvas.rotate(h * fr * FRACTURE_ROT * flip, cx, cy)
            s, top, bot, pad = h_slant * size, base - 1.3 * size, base + 0.5 * size, 0.5 * size
            edge = lx - pad if h < 0 else lx + w + pad
            path = skia.Path()
            path.moveTo(edge, top)
            path.lineTo(cx + s, top)
            path.lineTo(cx - s, bot)
            path.lineTo(edge, bot)
            path.close()
            canvas.clipPath(path, doAntiAlias=True)
        if chroma:
            r, g, b = color
            for c, off in (((r, 0, 0), -chroma), ((0, g, 0), 0.0), ((0, 0, b), chroma)):
                canvas.drawTextBlob(blob, lx + off, base, skia.Paint(
                    AntiAlias=True, Color=_argb(c, 1.0), BlendMode=skia.BlendMode.kPlus))
        else:
            canvas.drawTextBlob(blob, lx, base, skia.Paint(AntiAlias=True, Color=_argb(color, 1.0)))
        canvas.restore()


def _enter(canvas, st: LetterState, cx, cy, size):
    canvas.save()
    canvas.translate(cx + st.dx * size, cy + st.dy * size)
    if st.rot_deg:
        canvas.rotate(st.rot_deg)
    if st.scale != 1.0:
        canvas.scale(st.scale, st.scale)
    canvas.translate(-cx, -cy)


def _star(path: skia.Path, x, y, r):
    path.moveTo(x, y - r)
    path.quadTo(x, y, x + r, y)
    path.quadTo(x, y, x, y + r)
    path.quadTo(x, y, x - r, y)
    path.quadTo(x, y, x, y - r)
    path.close()


def _draw_particle(canvas: skia.Canvas, q, px: float, py: float, r: float, halo) -> None:
    """Render one emitted particle (dust, spark, petal, firefly, leaf, or snowflake)."""
    if q.kind == "spark":  # estrella de 4 puntas que crece con el destello, sobre un punto suave
        canvas.drawCircle(px, py, r * 0.6, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.7), MaskFilter=halo))
        star = skia.Path()
        _star(star, px, py, r * (0.5 + 0.5 * q.alpha))
        canvas.drawPath(star, skia.Paint(AntiAlias=True, Color=_argb(q.color, min(1.0, q.alpha * 1.3))))
    elif q.kind == "firefly":
        canvas.drawCircle(px, py, r * 2.6, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.45), MaskFilter=halo))
        canvas.drawCircle(px, py, r * 1.25, skia.Paint(AntiAlias=True, Color=_argb(q.color, min(1.0, q.alpha * 0.85))))
        canvas.drawCircle(px, py, r * 0.65, skia.Paint(AntiAlias=True, Color=_argb((255, 250, 225), min(1.0, q.alpha * 1.15))))
    elif q.kind == "petal":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        petal = skia.Path()
        petal.moveTo(0.0, -r * 1.35)
        petal.quadTo(r * 0.95, -r * 0.15, 0.0, r * 1.15)
        petal.quadTo(-r * 0.95, -r * 0.15, 0.0, -r * 1.35)
        petal.close()
        canvas.drawCircle(0.0, 0.0, r * 1.4, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.35), MaskFilter=halo))
        canvas.drawPath(petal, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha)))
        canvas.restore()
    elif q.kind == "leaf":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        leaf = skia.Path()
        leaf.moveTo(0.0, -r * 1.55)
        leaf.quadTo(r * 1.10, -r * 0.10, 0.0, r * 1.35)
        leaf.quadTo(-r * 1.10, -r * 0.10, 0.0, -r * 1.55)
        leaf.close()
        canvas.drawPath(leaf, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha)))
        rib = skia.Paint(AntiAlias=True, Color=_argb((255, 240, 210), q.alpha * 0.45),
                         Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.7, r * 0.18))
        canvas.drawLine(0.0, -r * 1.15, 0.0, r * 1.25, rib)
        canvas.restore()
    elif q.kind == "snow":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        canvas.drawCircle(0.0, 0.0, r * 1.3, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.32), MaskFilter=halo))
        stroke = skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha),
                            Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.9, r * 0.24),
                            StrokeCap=skia.Paint.kRound_Cap)
        arm = r * 1.15
        for deg in (0.0, 60.0, 120.0):
            rad = math.radians(deg)
            dx, dy = math.cos(rad) * arm, math.sin(rad) * arm
            canvas.drawLine(-dx, -dy, dx, dy, stroke)
        if r >= 2.2:
            canvas.drawCircle(0.0, 0.0, r * 0.28, skia.Paint(AntiAlias=True, Color=_argb((255, 255, 255), q.alpha)))
        canvas.restore()
    elif q.kind == "ember":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        canvas.drawCircle(0.0, 0.0, r * 2.1, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.55), MaskFilter=halo))
        ember = skia.Path()
        ember.moveTo(0.0, -r * 1.45)
        ember.lineTo(r * 0.72, 0.0)
        ember.lineTo(0.0, r * 1.10)
        ember.lineTo(-r * 0.72, 0.0)
        ember.close()
        canvas.drawPath(ember, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha)))
        canvas.drawCircle(0.0, 0.0, r * 0.45, skia.Paint(AntiAlias=True, Color=_argb((255, 245, 200), min(1.0, q.alpha * 1.1))))
        canvas.restore()
    elif q.kind in ("raindrop", "tear"):
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        top_mul = 2.05 if q.kind == "tear" else 1.65
        if q.kind == "tear":
            canvas.drawCircle(0.0, 0.0, r * 1.4, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.30), MaskFilter=halo))
        drop = skia.Path()
        drop.moveTo(0.0, -r * top_mul)
        drop.quadTo(r * 0.95, r * 0.20, 0.0, r * 1.10)
        drop.quadTo(-r * 0.95, r * 0.20, 0.0, -r * top_mul)
        drop.close()
        canvas.drawPath(drop, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.88)))
        canvas.drawCircle(-r * 0.18, r * 0.35, r * 0.24, skia.Paint(AntiAlias=True, Color=_argb((240, 250, 255), q.alpha * 0.75)))
        canvas.restore()
    elif q.kind == "ripple":
        rx, ry = r * 1.35, r * 0.36
        rect = skia.Rect.MakeLTRB(px - rx, py - ry, px + rx, py + ry)
        canvas.drawOval(rect, skia.Paint(
            AntiAlias=True, Color=_argb(q.color, q.alpha * 0.80), Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.8, r * 0.08)))
    elif q.kind == "bubble":
        canvas.drawCircle(px, py, r * 1.05, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.18)))
        canvas.drawCircle(px, py, r * 1.05, skia.Paint(
            AntiAlias=True, Color=_argb(q.color, q.alpha * 0.85), Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.8, r * 0.22)))
        canvas.drawCircle(px - r * 0.35, py - r * 0.35, r * 0.25, skia.Paint(AntiAlias=True, Color=_argb((255, 255, 255), q.alpha * 0.9)))
    elif q.kind == "mist":
        canvas.drawCircle(px, py, r * 1.4, skia.Paint(
            AntiAlias=True, Color=_argb(q.color, q.alpha * 0.42), MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, max(1.5, r * 0.7))))
    elif q.kind == "shard":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        shard = skia.Path()
        shard.moveTo(0.0, -r * 1.50)
        shard.lineTo(r * 0.85, r * 0.65)
        shard.lineTo(-r * 0.55, r * 1.10)
        shard.close()
        canvas.drawPath(shard, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.85)))
        canvas.drawLine(0.0, -r * 1.35, r * 0.65, r * 0.55, skia.Paint(
            AntiAlias=True, Color=_argb((255, 255, 255), q.alpha * 0.85), Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.7, r * 0.16)))
        canvas.restore()
    elif q.kind == "wisp":
        canvas.drawCircle(px, py, r * 2.0, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.38), MaskFilter=halo))
        canvas.drawCircle(px, py, r * 0.9, skia.Paint(
            AntiAlias=True, Color=_argb(q.color, q.alpha * 0.80), Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.7, r * 0.18)))
        canvas.drawCircle(px, py, r * 0.35, skia.Paint(AntiAlias=True, Color=_argb((255, 255, 255), q.alpha * 0.9)))
    elif q.kind == "seed":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        stroke = skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.90),
                            Style=skia.Paint.kStroke_Style, StrokeWidth=max(0.7, r * 0.18), StrokeCap=skia.Paint.kRound_Cap)
        canvas.drawLine(0.0, -r * 0.35, 0.0, r * 1.15, stroke)
        for ang in (-42.0, 0.0, 42.0):
            rad = math.radians(ang)
            canvas.drawLine(0.0, -r * 0.35, math.sin(rad) * r * 1.05, -r * 0.35 - math.cos(rad) * r * 1.05, stroke)
        canvas.restore()
    else:
        canvas.drawCircle(px, py, r * 1.8, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.5), MaskFilter=halo))
        canvas.drawCircle(px, py, r, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha)))


def draw_word_fx(canvas: skia.Canvas, fonts, fx: FxWord, letters: list[Letter], x: float, base: float, size: float,
                 t: float, rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onsets: list[float] | None = None,
                 backdrop: tuple[int, int, int] | None = None, carrier: CarrierWord | None = None,
                 origin_y: float | None = None) -> None:
    """`onsets`: segundo en que se canta cada letra (según las sílabas); las letras entran y destellan a ese ritmo.
    `backdrop`: color medio del fondo detrás de la palabra; el color se corrige para que contraste con él (sin cambiar el tono)."""
    n = len(letters)
    if n == 0:
        return
    p, m = adapt_params(fx.params, backdrop), fx.match
    b0, b1 = m.begin_ms / 1000.0, m.end_ms / 1000.0
    eff_spacing = max(-0.006, min(0.014, p.spacing_em)) * carrier.fill_front_u if carrier is not None else p.spacing_em
    gap = eff_spacing * size
    xs = [x + L.x + i * gap for i, L in enumerate(letters)]  # x absoluta de cada letra con el espaciado aplicado
    width = letters[-1].x + letters[-1].w + (n - 1) * gap
    blobs = [fonts.text(L.ch, size)[0] for L in letters]
    cy = origin_y if origin_y is not None else (base - CENTER_Y * size)
    chroma = (p.chromatic_em + 0.045 * strain_boost(p, strain)) * size  # +~2 px en los picos de tensión vocal (a 42 px)
    chroma = chroma if abs(chroma) >= CHROMA_MIN_PX else 0.0
    fr = 0.0 if reduce or p.fracture <= 0.0 else p.fracture * smoothstep(b0, b1 + 0.15, t)

    grain = p.grain > 0.0
    pad = 0.6 * size
    box = skia.Rect.MakeLTRB(x - pad, base - 1.6 * size, x + width + pad, base + 0.8 * size)
    if grain:  # capa propia para que el ruido solo caiga sobre píxeles de letra (kSrcATop)
        canvas.saveLayer(box, None)

    c_letters = carrier.letters if (carrier is not None and len(carrier.letters) == n) else None
    line_act = carrier.line_act if carrier is not None else None
    line_end_s = carrier.line_end_s if carrier is not None else None
    states = [
        letter_state(
            p, i, n, t, b0, b1, rms, reduce, strain,
            onsets[i] if onsets else None,
            carrier=c_letters[i] if c_letters else None,
            line_act=line_act,
            line_end_s=line_end_s,
        )
        for i in range(n)
    ]

    if p.trail > 0.0 and not reduce:  # estela: copias sencillas en las posiciones pasadas
        for delay, a0 in GHOSTS:
            for i in range(n):
                g = letter_state(
                    p, i, n, t - delay, b0, b1, rms, reduce, strain,
                    onsets[i] if onsets else None,
                    carrier=c_letters[i] if c_letters else None,
                    line_act=line_act,
                    line_end_s=line_end_s,
                )
                a = g.alpha * a0 * p.trail
                st = states[i]
                if a < 0.004 or (g.dx, g.dy, g.scale, g.rot_deg) == (st.dx, st.dy, st.scale, st.rot_deg):
                    continue  # quieta: el fantasma caería justo debajo de la letra
                _enter(canvas, g, xs[i] + letters[i].w / 2, cy, size)
                canvas.drawTextBlob(blobs[i], xs[i], base, skia.Paint(AntiAlias=True, Color=_argb(g.color, a)))
                canvas.restore()

    go = smoothstep(b0, b0 + 0.12, t)  # los ecos no se mueven hasta que se canta la palabra
    for dx, dy, a0 in (echoes(p, t, reduce) if p.trail > 0.0 else ()):  # ecos persistentes (turbulencia lenta del tiempo)
        dx, dy = dx * go, dy * go
        for i, st in enumerate(states):
            a = st.alpha * a0
            if a < 0.004:
                continue
            _enter(canvas, replace(st, dx=st.dx + dx, dy=st.dy + dy), xs[i] + letters[i].w / 2, cy, size)
            canvas.drawTextBlob(blobs[i], xs[i], base, skia.Paint(AntiAlias=True, Color=_argb(st.color, a)))
            canvas.restore()

    if any(s.glow > 0.02 for s in states):
        # imágenes blancas cacheadas con alpha por letra en una capa, y un solo teñido kSrcIn con el gradiente de color
        # (ColorFilter por imagen costaba ~0.5 ms cada una en CPU)
        canvas.saveLayer(box, None)
        for i, st in enumerate(states):
            a = min(1.0, st.glow * 0.7) * st.alpha
            if a < 0.01:
                continue
            img, pad_g = _glow_image(fonts, letters[i].ch, size, letters[i].w)
            _enter(canvas, st, xs[i] + letters[i].w / 2, cy, size)
            canvas.drawImage(img, xs[i] - pad_g, base - GLOW_ASC * size - pad_g, skia.SamplingOptions(), skia.Paint(Alphaf=a))
            canvas.restore()
        c0, c1 = states[0].color, states[-1].color
        tint = skia.Paint(BlendMode=skia.BlendMode.kSrcIn)
        if c0 == c1 or n == 1:
            tint.setColor(_argb(c0, 1.0))
        else:
            tint.setShader(skia.GradientShader.MakeLinear(
                [(xs[0] + letters[0].w / 2, 0), (xs[-1] + letters[-1].w / 2, 0)], [_argb(c0, 1.0), _argb(c1, 1.0)]))
        canvas.drawRect(box, tint)
        canvas.restore()

    # el desenfoque es común a todas las letras: una capa de palabra (el alpha de cada letra se dibuja dentro)
    sigma = states[0].blur_em * size * 0.5
    wblur = sigma * 2 > BLUR_MIN_PX
    if wblur:
        canvas.saveLayer(box, skia.Paint(ImageFilter=skia.ImageFilters.Blur(sigma, sigma)))
    for i, st in enumerate(states):
        if st.alpha < 0.004:
            continue
        L = letters[i]
        _enter(canvas, st, xs[i] + L.w / 2, cy, size)
        flip = 1.0 if p.seed >> i & 1 else -1.0  # sentido de giro de cada letra al partirse, estable por semilla
        slant = (((p.seed >> (i + 3)) & 7) - 3.5) * 0.04  # inclinación del corte en em
        chroma_i = chroma * c_letters[i].fill_p if c_letters is not None else chroma
        if abs(chroma_i) >= CHROMA_MIN_PX:
            e = 0.4 * size + abs(chroma_i)
            canvas.saveLayer(skia.Rect.MakeLTRB(xs[i] - e, base - 1.3 * size - e, xs[i] + L.w + e, base + 0.5 * size + e),
                             skia.Paint(Alphaf=st.alpha))
            _body(canvas, blobs[i], xs[i], base, size, L.w, st.color, fr, chroma_i, flip, slant)
            canvas.restore()
        elif fr >= 0.01:  # las mitades se solapan en el borde: alpha por capa para no doblarlo
            canvas.saveLayer(None, skia.Paint(Alphaf=st.alpha))
            _body(canvas, blobs[i], xs[i], base, size, L.w, st.color, fr, 0.0, flip, slant)
            canvas.restore()
        else:
            canvas.drawTextBlob(blobs[i], xs[i], base, skia.Paint(AntiAlias=True, Color=_argb(st.color, st.alpha)))
        canvas.restore()
    if wblur:
        canvas.restore()

    if p.particles > 0 or p.sparkle > 0:
        halo = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, HALO_SIGMA * size)
        c_onsets = [cl.onset_s for cl in c_letters] if c_letters else onsets
        front_u = carrier.fill_front_u if carrier is not None else None
        for q in particles(p, [(xi - x) / size for xi in xs], [L.w / size for L in letters], t, b0, b1, reduce,
                           onsets=c_onsets, fill_front_u=front_u):
            if q.alpha < 0.01:
                continue
            _draw_particle(canvas, q, x + q.x_em * size, base + q.y_em * size, q.r_em * size, halo)

    if grain:
        frame = int(t * 24) if t >= 0 else -int(-t * 24) - 1
        k = (p.seed ^ (frame * 0x9E3779B1)) & 0xFFFFFFFF
        tile = _grain_tile(k % GRAIN_VARIANTS)
        off = (k >> 8) % GRAIN_TILE  # desplazamiento extra para que dos fotogramas con la misma tesela difieran
        shader = tile.makeShader(skia.TileMode.kRepeat, skia.TileMode.kRepeat, skia.SamplingOptions(),
                                 skia.Matrix.Translate(off, (k >> 16) % GRAIN_TILE))
        canvas.drawRect(box, skia.Paint(
            Shader=shader, Alphaf=min(1.0, p.grain * 0.35), BlendMode=skia.BlendMode.kSrcATop))
        canvas.restore()
