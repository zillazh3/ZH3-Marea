"""Dibujo skia de los efectos por palabra. La escena abre la capa y aplica después su máscara de cantado (kDstIn):
aquí se dibujan las letras en su color final, sin fondo ni máscara. Todo es función de (t, FxParams, índice de letra)."""
import math
from dataclasses import dataclass, replace
from functools import lru_cache

import skia

from marea.wordfx.anim import LetterState, echoes, letter_state, particles, smoothstep, strain_boost
from marea.wordfx.contrast import adapt_params
from marea.wordfx.fxtypes import FxWord

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


def draw_word_fx(canvas: skia.Canvas, fonts, fx: FxWord, letters: list[Letter], x: float, base: float, size: float,
                 t: float, rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onsets: list[float] | None = None, backdrop: tuple[int, int, int] | None = None) -> None:
    """`onsets`: segundo en que se canta cada letra (según las sílabas); las letras entran y destellan a ese ritmo.
    `backdrop`: color medio del fondo detrás de la palabra; el color se corrige para que contraste con él (sin cambiar el tono)."""
    n = len(letters)
    if n == 0:
        return
    p, m = adapt_params(fx.params, backdrop), fx.match
    b0, b1 = m.begin_ms / 1000.0, m.end_ms / 1000.0
    gap = p.spacing_em * size
    xs = [x + L.x + i * gap for i, L in enumerate(letters)]  # x absoluta de cada letra con el espaciado aplicado
    width = letters[-1].x + letters[-1].w + (n - 1) * gap
    blobs = [fonts.text(L.ch, size)[0] for L in letters]
    cy = base - CENTER_Y * size
    chroma = (p.chromatic_em + 0.045 * strain_boost(p, strain)) * size  # +~2 px en los picos de tensión vocal (a 42 px)
    chroma = chroma if abs(chroma) >= CHROMA_MIN_PX else 0.0
    fr = 0.0 if reduce or p.fracture <= 0.0 else p.fracture * smoothstep(b0, b1 + 0.15, t)

    grain = p.grain > 0.0
    pad = 0.6 * size
    box = skia.Rect.MakeLTRB(x - pad, base - 1.6 * size, x + width + pad, base + 0.8 * size)
    if grain:  # capa propia para que el ruido solo caiga sobre píxeles de letra (kSrcATop)
        canvas.saveLayer(box, None)

    states = [letter_state(p, i, n, t, b0, b1, rms, reduce, strain, onsets[i] if onsets else None) for i in range(n)]

    if p.trail > 0.0 and not reduce:  # estela: copias sencillas en las posiciones pasadas
        for delay, a0 in GHOSTS:
            for i in range(n):
                g = letter_state(p, i, n, t - delay, b0, b1, rms, reduce, strain, onsets[i] if onsets else None)
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
        if chroma:
            e = 0.4 * size + abs(chroma)
            canvas.saveLayer(skia.Rect.MakeLTRB(xs[i] - e, base - 1.3 * size - e, xs[i] + L.w + e, base + 0.5 * size + e),
                             skia.Paint(Alphaf=st.alpha))
            _body(canvas, blobs[i], xs[i], base, size, L.w, st.color, fr, chroma, flip, slant)
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
        for q in particles(p, [(xi - x) / size for xi in xs], [L.w / size for L in letters], t, b0, b1, reduce):
            if q.alpha < 0.01:
                continue
            px, py, r = x + q.x_em * size, base + q.y_em * size, q.r_em * size
            if q.kind == "spark":  # estrella de 4 puntas que crece con el destello, sobre un punto suave
                canvas.drawCircle(px, py, r * 0.6, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.7), MaskFilter=halo))
                star = skia.Path()
                _star(star, px, py, r * (0.5 + 0.5 * q.alpha))
                canvas.drawPath(star, skia.Paint(AntiAlias=True, Color=_argb(q.color, min(1.0, q.alpha * 1.3))))
            else:
                canvas.drawCircle(px, py, r * 1.8, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha * 0.5), MaskFilter=halo))
                canvas.drawCircle(px, py, r, skia.Paint(AntiAlias=True, Color=_argb(q.color, q.alpha)))

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
