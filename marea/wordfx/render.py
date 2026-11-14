"""Dibujo skia de los efectos por palabra. La escena abre la capa y aplica después su máscara de cantado (kDstIn):
aquí se dibujan las letras en su color final, sin fondo ni máscara. Todo es función de (t, FxParams, índice de letra)."""
import math
import threading
from dataclasses import dataclass
from functools import lru_cache

import skia

from marea.wordfx.anim import LetterState, echoes, letter_state, particles, smoothstep, strain_boost
from marea.wordfx.contrast import adapt_params
from marea.wordfx.fxtypes import CarrierWord, FxWord

GHOSTS = ((0.12, 0.1), (0.08, 0.2), (0.04, 0.35))  # (retraso s, alpha base): del más viejo al más reciente
GLOW_ASC, GLOW_DESC = 1.0, 0.4  # em sobre/bajo la línea base que cubre la imagen del glow
_GLOW_SIGMAS = (0.085, 0.135, 0.215)  # tight | standard | wide atmospheric bloom tiers (em)
HALO_SIGMA = 0.035  # em; halo suave de cada mota
GRAIN_VARIANTS = 16
GRAIN_TILE = 128
CENTER_Y = 0.35  # em sobre la línea base: centro vertical en el que giran/escalan las letras
FRACTURE_GAP = 0.08  # em de separación final entre mitades
FRACTURE_ROT = 4.0  # grados por mitad a fracture=1
CHROMA_MIN_PX = 0.3
BLUR_MIN_PX = 0.4


@dataclass(frozen=True, slots=True)
class Letter:
    ch: str
    x: float  # px desde el borde izquierdo de la palabra
    w: float  # avance en px


_SAMP_DEFAULT = skia.SamplingOptions()
_SAMP_LINEAR = skia.SamplingOptions(skia.FilterMode.kLinear)


@lru_cache(maxsize=256)
def _blur_mask(sigma_q: float):
    return skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma_q)


@lru_cache(maxsize=128)
def _word_blur_filter(sigma_q: float):
    return skia.ImageFilters.Blur(sigma_q, sigma_q)


@lru_cache(maxsize=512)
def _tint_cf(argb_int: int):
    return skia.ColorFilters.Blend(argb_int, skia.BlendMode.kSrcIn)


@lru_cache(maxsize=2048)
def _letters_cached(fonts, text: str, size: float) -> tuple[Letter, ...]:
    """Cached glyph advances per character (un-kerned, matching `Scene._glyphs`)."""
    out, x = [], 0.0
    for ch in text:
        w = fonts.text(ch, size)[1]
        out.append(Letter(ch, x, w))
        x += w
    return tuple(out)


def letters_for(fonts, text: str, size: float) -> list[Letter]:
    """Geometría por carácter: anchos acumulados, sin kerning (misma convención que Scene._glyphs)."""
    return list(_letters_cached(fonts, text, size))


@lru_cache(maxsize=2048)
def _word_blobs(fonts, text: str, size: float) -> tuple[skia.TextBlob, ...]:
    return tuple(fonts.text(ch, size)[0] for ch in text)


@lru_cache(maxsize=240)  # ~0.4 MB max at 60 px across 3 spread tiers
def _glow_image(fonts, ch: str, size: float, w: float, tier: int = 1) -> tuple[skia.Image, int]:
    """Pre-blurred white glyph sprite cached by `(fonts, ch, size, tier)` (zero per-frame blur cost)."""
    sigma = _GLOW_SIGMAS[tier] * size
    pad = math.ceil(3 * sigma)
    surf = skia.Surface(math.ceil(w) + 2 * pad, math.ceil((GLOW_ASC + GLOW_DESC) * size) + 2 * pad)
    surf.getCanvas().drawTextBlob(fonts.text(ch, size)[0], pad, pad + GLOW_ASC * size, skia.Paint(
        AntiAlias=True, Color=skia.ColorWHITE, MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma)))
    return surf.makeImageSnapshot(), pad


def _glow_tier(glow_spread: float) -> int:
    if glow_spread < 0.30:
        return 0
    if glow_spread < 0.60:
        return 1
    return 2


def _backdrop_dark_factor(backdrop: tuple[int, int, int] | None) -> float:
    """Return 1.0 on dark backdrops and ramp smoothly to 0.0 on bright backgrounds (>= 0.38 luma)."""
    if backdrop is None:
        return 1.0
    luma = (0.2126 * backdrop[0] + 0.7152 * backdrop[1] + 0.0722 * backdrop[2]) / 255.0
    if luma >= 0.38:
        return 0.0
    return (0.38 - luma) / 0.38


class _RenderScratch:
    """Thread-local reusable Skia Paint and Path objects to eliminate per-particle allocations."""
    __slots__ = ("fill", "stroke", "halo", "core", "path1", "path2")

    def __init__(self) -> None:
        self.fill = skia.Paint(AntiAlias=True)
        self.stroke = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style)
        self.halo = skia.Paint(AntiAlias=True)
        self.core = skia.Paint(AntiAlias=True)
        self.path1 = skia.Path()
        self.path2 = skia.Path()


_TLS = threading.local()


def _scratch() -> _RenderScratch:
    sc = getattr(_TLS, "scratch", None)
    if sc is None:
        sc = _RenderScratch()
        _TLS.scratch = sc
    return sc


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


def _enter(canvas, st: LetterState, cx, cy, size, dx_extra: float = 0.0, dy_extra: float = 0.0):
    canvas.save()
    canvas.translate(cx + (st.dx + dx_extra) * size, cy + (st.dy + dy_extra) * size)
    if st.rot_deg:
        canvas.rotate(st.rot_deg)
    if st.scale != 1.0:
        canvas.scale(st.scale, st.scale)
    canvas.translate(-cx, -cy)


def _star(path: skia.Path, x: float, y: float, rx: float, ry: float | None = None, waist: float = 0.0) -> None:
    ry = rx if ry is None else ry
    wx, wy = rx * waist, ry * waist
    path.rewind()
    path.moveTo(x, y - ry)
    path.quadTo(x + wx, y - wy, x + rx, y)
    path.quadTo(x + wx, y + wy, x, y + ry)
    path.quadTo(x - wx, y + wy, x - rx, y)
    path.quadTo(x - wx, y - wy, x, y - ry)
    path.close()


def _draw_spark(canvas: skia.Canvas, q, px: float, py: float, r: float, size: float,
                halo, dark_factor: float, sc: _RenderScratch, alpha_scale: float) -> None:
    a = min(1.0, q.alpha * alpha_scale)
    if a < 0.01:
        return
    style = getattr(q, "style", "") or "star"
    fill, halo_p, core, stroke, path = sc.fill, sc.halo, sc.core, sc.stroke, sc.path1
    halo_p.setMaskFilter(halo)
    plus_mode = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
    core_col = getattr(q, "core_color", None) or (255, 255, 255)

    if style == "celestial":
        # Wide soft outer aura + razor-sharp astronomical diffraction star + crisp pinhole core
        halo_p.setBlendMode(plus_mode)
        halo_p.setColor(_argb(q.color, a * (0.30 + 0.16 * dark_factor)))
        canvas.drawCircle(px, py, r * 2.15, halo_p)
        arm = r * (0.72 + 0.45 * a)
        _star(path, px, py, arm, arm, waist=0.05)
        fill.setBlendMode(plus_mode)
        fill.setColor(_argb(q.color, min(1.0, a * 1.15)))
        canvas.drawPath(path, fill)
        if r >= 0.11 * size:
            # Subtle 45-deg micro diffraction cross on hero stars
            d = arm * 0.40
            stroke.setBlendMode(plus_mode)
            stroke.setColor(_argb(core_col, a * 0.60))
            stroke.setStrokeWidth(max(0.7, r * 0.08))
            stroke.setStrokeCap(skia.Paint.kRound_Cap)
            canvas.drawLine(px - d, py - d, px + d, py + d, stroke)
            canvas.drawLine(px - d, py + d, px + d, py - d, stroke)
        core.setBlendMode(plus_mode)
        core.setColor(_argb(core_col, min(1.0, a * 1.25)))
        canvas.drawCircle(px, py, max(0.8, r * 0.26), core)
    elif style == "ember":
        halo_p.setBlendMode(plus_mode)
        halo_p.setColor(_argb(q.color, a * 0.50))
        canvas.drawCircle(px, py, r * 1.85, halo_p)
        vy = getattr(q, "vy_em", -0.5)
        tail_dy = -vy * 0.08 * size
        stroke.setBlendMode(plus_mode)
        stroke.setColor(_argb(q.color, a * 0.78))
        stroke.setStrokeWidth(max(0.9, r * 0.50))
        stroke.setStrokeCap(skia.Paint.kRound_Cap)
        canvas.drawLine(px, py, px, py + tail_dy, stroke)
        core.setBlendMode(plus_mode)
        core.setColor(_argb(core_col, min(1.0, a * 1.2)))
        canvas.drawCircle(px, py, max(0.9, r * 0.42), core)
    elif style == "buoyant":
        halo_p.setBlendMode(plus_mode)
        halo_p.setColor(_argb(q.color, a * 0.38))
        canvas.drawCircle(px, py, r * 1.55, halo_p)
        _star(path, px, py, r * 0.85, r * (0.75 + 0.55 * a), waist=0.06)
        fill.setBlendMode(plus_mode)
        fill.setColor(_argb(q.color, min(1.0, a * 1.15)))
        canvas.drawPath(path, fill)
        core.setBlendMode(plus_mode)
        core.setColor(_argb(core_col, min(1.0, a * 1.15)))
        canvas.drawCircle(px, py, max(0.8, r * 0.30), core)
    elif style == "bokeh":
        halo_p.setBlendMode(plus_mode)
        halo_p.setColor(_argb(q.color, a * 0.36))
        canvas.drawCircle(px, py, r * 1.75, halo_p)
        fill.setBlendMode(skia.BlendMode.kSrcOver)
        fill.setColor(_argb(q.color, a * 0.42))
        canvas.drawCircle(px, py, r * 0.88, fill)
        core.setBlendMode(plus_mode)
        core.setColor(_argb(core_col, a * 0.68))
        canvas.drawCircle(px, py, max(0.8, r * 0.36), core)
    elif style == "flare":
        halo_p.setBlendMode(plus_mode)
        halo_p.setColor(_argb(q.color, a * 0.48))
        canvas.drawOval(skia.Rect.MakeLTRB(px - r * 1.9, py - r * 0.28, px + r * 1.9, py + r * 0.28), halo_p)
        core.setBlendMode(plus_mode)
        core.setColor(_argb(core_col, a * 0.82))
        canvas.drawCircle(px, py, max(0.9, r * 0.52), core)
    else:
        halo_p.setBlendMode(skia.BlendMode.kSrcOver)
        halo_p.setColor(_argb(q.color, a * 0.7))
        canvas.drawCircle(px, py, r * 0.6, halo_p)
        _star(path, px, py, r * (0.5 + 0.5 * a))
        fill.setBlendMode(skia.BlendMode.kSrcOver)
        fill.setColor(_argb(q.color, min(1.0, a * 1.3)))
        canvas.drawPath(path, fill)


def _draw_particle(canvas: skia.Canvas, q, px: float, py: float, r: float, size: float,
                   halo, dark_factor: float, sc: _RenderScratch, alpha_scale: float = 1.0) -> None:
    """Render one emitted particle using pooled Skia Paint/Path scratch objects."""
    a = min(1.0, q.alpha * alpha_scale)
    if a < 0.01:
        return
    if q.kind == "spark":
        _draw_spark(canvas, q, px, py, r, size, halo, dark_factor, sc, alpha_scale)
        return

    fill, stroke, halo_p, core, path = sc.fill, sc.stroke, sc.halo, sc.core, sc.path1
    fill.setBlendMode(skia.BlendMode.kSrcOver)
    stroke.setBlendMode(skia.BlendMode.kSrcOver)
    halo_p.setBlendMode(skia.BlendMode.kSrcOver)
    halo_p.setMaskFilter(halo)
    core.setBlendMode(skia.BlendMode.kSrcOver)

    if q.kind == "firefly":
        halo_p.setColor(_argb(q.color, a * 0.45))
        canvas.drawCircle(px, py, r * 2.6, halo_p)
        fill.setColor(_argb(q.color, min(1.0, a * 0.85)))
        canvas.drawCircle(px, py, r * 1.25, fill)
        core.setColor(_argb((255, 250, 225), min(1.0, a * 1.15)))
        canvas.drawCircle(px, py, r * 0.65, core)
    elif q.kind == "petal":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        path.rewind()
        path.moveTo(0.0, -r * 1.35)
        path.quadTo(r * 0.95, -r * 0.15, 0.0, r * 1.15)
        path.quadTo(-r * 0.95, -r * 0.15, 0.0, -r * 1.35)
        path.close()
        halo_p.setColor(_argb(q.color, a * 0.35))
        canvas.drawCircle(0.0, 0.0, r * 1.4, halo_p)
        fill.setColor(_argb(q.color, a))
        canvas.drawPath(path, fill)
        canvas.restore()
    elif q.kind == "leaf":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        path.rewind()
        path.moveTo(0.0, -r * 1.55)
        path.quadTo(r * 1.10, -r * 0.10, 0.0, r * 1.35)
        path.quadTo(-r * 1.10, -r * 0.10, 0.0, -r * 1.55)
        path.close()
        fill.setColor(_argb(q.color, a))
        canvas.drawPath(path, fill)
        stroke.setColor(_argb((255, 240, 210), a * 0.45))
        stroke.setStrokeWidth(max(0.7, r * 0.18))
        canvas.drawLine(0.0, -r * 1.15, 0.0, r * 1.25, stroke)
        canvas.restore()
    elif q.kind == "snow":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        halo_p.setColor(_argb(q.color, a * 0.32))
        canvas.drawCircle(0.0, 0.0, r * 1.3, halo_p)
        stroke.setColor(_argb(q.color, a))
        stroke.setStrokeWidth(max(0.9, r * 0.24))
        stroke.setStrokeCap(skia.Paint.kRound_Cap)
        arm = r * 1.15
        for deg in (0.0, 60.0, 120.0):
            rad = math.radians(deg)
            dx, dy = math.cos(rad) * arm, math.sin(rad) * arm
            canvas.drawLine(-dx, -dy, dx, dy, stroke)
        if r >= 2.2:
            core.setColor(_argb((255, 255, 255), a))
            canvas.drawCircle(0.0, 0.0, r * 0.28, core)
        canvas.restore()
    elif q.kind == "ember":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        # Wide warm flame aura + organic teardrop flame lick + bright luminous core
        halo_p.setBlendMode(skia.BlendMode.kPlus if dark_factor > 0.2 else skia.BlendMode.kSrcOver)
        halo_p.setColor(_argb(q.color, a * (0.46 + 0.16 * dark_factor)))
        canvas.drawCircle(0.0, 0.0, r * 2.4, halo_p)
        path.rewind()
        path.moveTo(0.0, -r * 1.65)
        path.quadTo(r * 0.92, r * 0.24, 0.0, r * 0.92)
        path.quadTo(-r * 0.92, r * 0.24, 0.0, -r * 1.65)
        path.close()
        fill.setColor(_argb(q.color, a))
        canvas.drawPath(path, fill)
        core_col = getattr(q, "core_color", None) or (255, 245, 200)
        core.setBlendMode(skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver)
        core.setColor(_argb(core_col, min(1.0, a * 1.15)))
        canvas.drawCircle(0.0, r * 0.18, max(0.8, r * 0.48), core)
        canvas.restore()
    elif q.kind in ("raindrop", "tear", "crimson"):
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        stretch = 1.0 + min(0.45, abs(getattr(q, "vy_em", 0.0)) * 0.18) if q.kind == "raindrop" else 1.0
        top_mul = (2.05 if q.kind in ("tear", "crimson") else 1.65) * stretch
        if q.kind in ("tear", "crimson"):
            halo_p.setColor(_argb(q.color, a * 0.34))
            canvas.drawCircle(0.0, 0.0, r * 1.45, halo_p)
        path.rewind()
        path.moveTo(0.0, -r * top_mul)
        path.quadTo(r * 0.98, r * 0.22, 0.0, r * 1.12)
        path.quadTo(-r * 0.98, r * 0.22, 0.0, -r * top_mul)
        path.close()
        fill.setColor(_argb(q.color, a * (0.94 if q.kind == "crimson" else 0.88)))
        canvas.drawPath(path, fill)
        glint = (255, 200, 210) if q.kind == "crimson" else (240, 250, 255)
        core.setColor(_argb(glint, a * 0.75))
        canvas.drawCircle(-r * 0.18, r * 0.35, r * 0.24, core)
        canvas.restore()
    elif q.kind in ("ripple", "stain"):
        rx, ry = r * 1.35, r * 0.36
        rect = skia.Rect.MakeLTRB(px - rx, py - ry, px + rx, py + ry)
        if q.kind == "stain":
            fill.setColor(_argb(q.color, a * 0.42))
            canvas.drawOval(rect, fill)
        stroke.setColor(_argb(q.color, a * 0.80))
        stroke.setStrokeWidth(max(0.8, r * 0.08))
        canvas.drawOval(rect, stroke)
    elif q.kind == "bubble":
        fill.setColor(_argb(q.color, a * 0.18))
        canvas.drawCircle(px, py, r * 1.05, fill)
        stroke.setColor(_argb(q.color, a * 0.85))
        stroke.setStrokeWidth(max(0.8, r * 0.22))
        canvas.drawCircle(px, py, r * 1.05, stroke)
        core.setColor(_argb((255, 255, 255), a * 0.9))
        canvas.drawCircle(px - r * 0.35, py - r * 0.35, r * 0.25, core)
    elif q.kind == "mist":
        mf = _blur_mask(round(max(1.5, r * 0.65), 2))
        halo_p.setMaskFilter(mf)
        halo_p.setColor(_argb(q.color, a * 0.32))
        canvas.drawCircle(px - r * 0.32, py + r * 0.10, r * 1.18, halo_p)
        halo_p.setColor(_argb(q.color, a * 0.28))
        canvas.drawCircle(px + r * 0.34, py - r * 0.10, r * 0.96, halo_p)
    elif q.kind == "shard":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        path.rewind()
        path.moveTo(0.0, -r * 1.50)
        path.lineTo(r * 0.85, r * 0.65)
        path.lineTo(-r * 0.55, r * 1.10)
        path.close()
        fill.setColor(_argb(q.color, a * 0.85))
        canvas.drawPath(path, fill)
        stroke.setColor(_argb((255, 255, 255), a * 0.85))
        stroke.setStrokeWidth(max(0.7, r * 0.16))
        canvas.drawLine(0.0, -r * 1.35, r * 0.65, r * 0.55, stroke)
        canvas.restore()
    elif q.kind == "wisp":
        # Luminous soft head + two trailing comet beads along opposite velocity
        vx, vy = getattr(q, "vx_em", 0.0), getattr(q, "vy_em", 0.0)
        sp = math.hypot(vx, vy)
        ux, uy = (vx / sp, vy / sp) if sp > 1e-4 else (0.0, -1.0)
        plus_w = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        fill.setBlendMode(plus_w)
        for step, r_mul, a_mul in ((0.11, 0.38, 0.22), (0.055, 0.62, 0.44)):
            fill.setColor(_argb(q.color, a * a_mul))
            canvas.drawCircle(px - ux * step * size, py - uy * step * size, r * r_mul, fill)
        halo_p.setBlendMode(plus_w)
        halo_p.setColor(_argb(q.color, a * 0.38))
        canvas.drawCircle(px, py, r * 1.85, halo_p)
        fill.setColor(_argb(q.color, a * 0.88))
        canvas.drawCircle(px, py, r * 0.68, fill)
        core.setBlendMode(plus_w)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 255, 255), a * 0.95))
        canvas.drawCircle(px, py, max(0.8, r * 0.28), core)
    elif q.kind == "seed":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        stroke.setColor(_argb(q.color, a * 0.90))
        stroke.setStrokeWidth(max(0.7, r * 0.18))
        stroke.setStrokeCap(skia.Paint.kRound_Cap)
        canvas.drawLine(0.0, -r * 0.35, 0.0, r * 1.15, stroke)
        for ang in (-42.0, 0.0, 42.0):
            rad = math.radians(ang)
            canvas.drawLine(0.0, -r * 0.35, math.sin(rad) * r * 1.05, -r * 0.35 - math.cos(rad) * r * 1.05, stroke)
        canvas.restore()
    elif q.kind == "prism":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.38))
        canvas.drawCircle(0.0, 0.0, r * 1.7, halo_p)
        _star(path, 0.0, 0.0, r * 0.90, r * 1.45, waist=0.45)
        fill.setBlendMode(plus_p)
        fill.setColor(_argb(q.color, a * 0.88))
        canvas.drawPath(path, fill)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 250, 235), a))
        canvas.drawCircle(0.0, 0.0, max(0.7, r * 0.26), core)
        canvas.restore()
    elif q.kind == "candle":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.45))
        canvas.drawCircle(px, py, r * 2.1, halo_p)
        fill.setBlendMode(skia.BlendMode.kSrcOver)
        fill.setColor(_argb(q.color, a * 0.82))
        canvas.drawCircle(px, py, r * 0.85, fill)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 242, 228), a))
        canvas.drawCircle(px, py, max(0.8, r * 0.38), core)
    elif q.kind == "gale":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        stroke.setBlendMode(plus_p)
        stroke.setStrokeCap(skia.Paint.kRound_Cap)
        stroke.setStrokeWidth(max(0.8, r * 0.36))
        stroke.setColor(_argb(q.color, a * 0.72))
        canvas.drawLine(-r * 2.2, 0.0, r * 0.6, 0.0, stroke)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (242, 255, 215), a * 0.95))
        canvas.drawCircle(r * 0.6, 0.0, max(0.8, r * 0.40), core)
        canvas.restore()
    elif q.kind == "pollen":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.42))
        canvas.drawCircle(px, py, r * 1.9, halo_p)
        fill.setBlendMode(skia.BlendMode.kSrcOver)
        fill.setColor(_argb(q.color, a * 0.92))
        canvas.drawCircle(px, py, r * 0.75, fill)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 250, 215), a))
        canvas.drawCircle(px, py, max(0.7, r * 0.34), core)
    elif q.kind == "corona":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.48))
        canvas.drawCircle(px, py, r * 2.3, halo_p)
        _star(path, px, py, r * 1.05, r * 0.65, waist=0.18)
        fill.setBlendMode(plus_p)
        fill.setColor(_argb(q.color, a * 0.86))
        canvas.drawPath(path, fill)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 252, 224), a))
        canvas.drawCircle(px, py, max(0.8, r * 0.35), core)
    elif q.kind == "starburst":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        vx, vy = getattr(q, "vx_em", 0.0), getattr(q, "vy_em", -0.5)
        tail_x, tail_y = -vx * 0.14 * size, -vy * 0.14 * size
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.48))
        canvas.drawCircle(px, py, r * 2.0, halo_p)
        stroke.setBlendMode(plus_p)
        stroke.setStrokeCap(skia.Paint.kRound_Cap)
        stroke.setStrokeWidth(max(0.9, r * 0.42))
        stroke.setColor(_argb(q.color, a * 0.82))
        canvas.drawLine(px, py, px + tail_x, py + tail_y, stroke)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 248, 205), min(1.0, a * 1.15)))
        canvas.drawCircle(px, py, max(0.9, r * 0.45), core)
    elif q.kind == "harvest":
        canvas.save()
        canvas.translate(px, py)
        canvas.rotate(q.rot_deg)
        halo_p.setColor(_argb(q.color, a * 0.30))
        canvas.drawCircle(0.0, 0.0, r * 1.5, halo_p)
        fill.setColor(_argb(q.color, a * 0.90))
        canvas.drawOval(skia.Rect.MakeLTRB(-r * 1.25, -r * 0.42, r * 1.25, r * 0.42), fill)
        canvas.restore()
    elif q.kind == "equinox":
        halo_p.setColor(_argb(q.color, a * 0.36))
        canvas.drawCircle(px, py, r * 1.85, halo_p)
        fill.setColor(_argb(q.color, a * 0.78))
        canvas.drawCircle(px, py, r * 0.82, fill)
        core.setColor(_argb(getattr(q, "core_color", None) or (240, 255, 250), a * 0.85))
        canvas.drawCircle(px, py, max(0.7, r * 0.30), core)
    elif q.kind == "spectral":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.40))
        canvas.drawCircle(px, py, r * 2.0, halo_p)
        fill.setBlendMode(skia.BlendMode.kSrcOver)
        fill.setColor(_argb(q.color, a * 0.75))
        canvas.drawCircle(px, py, r * 0.80, fill)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (248, 232, 255), a * 0.90))
        canvas.drawCircle(px, py, max(0.7, r * 0.32), core)
    elif q.kind == "lantern":
        plus_p = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
        halo_p.setBlendMode(plus_p)
        halo_p.setColor(_argb(q.color, a * 0.48))
        canvas.drawCircle(px, py, r * 2.4, halo_p)
        fill.setBlendMode(skia.BlendMode.kSrcOver)
        fill.setColor(_argb(q.color, a * 0.88))
        canvas.drawRoundRect(skia.Rect.MakeLTRB(px - r * 0.72, py - r * 0.95, px + r * 0.72, py + r * 0.95),
                             r * 0.48, r * 0.48, fill)
        core.setBlendMode(plus_p)
        core.setColor(_argb(getattr(q, "core_color", None) or (255, 242, 198), min(1.0, a * 1.15)))
        canvas.drawCircle(px, py + r * 0.25, max(0.8, r * 0.38), core)
    else:
        halo_p.setColor(_argb(q.color, a * 0.5))
        canvas.drawCircle(px, py, r * 1.8, halo_p)
        fill.setColor(_argb(q.color, a))
        canvas.drawCircle(px, py, r, fill)


def _draw_motif(canvas: skia.Canvas, p, x: float, base: float, width: float, size: float,
                t: float, b0: float, b1: float, dark_factor: float, carrier: CarrierWord | None,
                wipe_alpha: float, reduce: bool) -> None:
    """Render subtle, pooled word-level atmospheric motif behind glyphs without obscuring text."""
    if not p.motif or reduce or wipe_alpha <= 0.01 or width <= 0.0:
        return
    front_u = carrier.fill_front_u if carrier is not None else smoothstep(b0, b1, t)
    act = smoothstep(b0 - 0.15, b0 + 0.12, t) * (1.0 - 0.85 * smoothstep(b1, b1 + p.release_s, t))
    if carrier is not None:
        act *= min(1.0, max(0.0, carrier.line_act))
    if act < 0.01:
        return
    sc = _scratch()
    plus_m = skia.BlendMode.kPlus if dark_factor > 0.25 else skia.BlendMode.kSrcOver
    a_base = act * wipe_alpha * (0.45 + 0.55 * dark_factor)
    gcol = p.glow_color if p.glow_color is not None else p.color_from
    motif = p.motif

    if motif == "threshold":
        slit_env = math.sin(math.pi * min(1.0, max(0.0, front_u)))
        a = a_base * slit_env * 0.45
        if a < 0.01:
            return
        px = x + front_u * width
        sc.halo.setBlendMode(plus_m)
        sc.halo.setMaskFilter(_blur_mask(round(0.08 * size, 2)))
        sc.halo.setColor(_argb(gcol, a * 0.65))
        canvas.drawOval(skia.Rect.MakeLTRB(px - 0.11 * size, base - 0.98 * size,
                                           px + 0.11 * size, base + 0.24 * size), sc.halo)
        sc.stroke.setBlendMode(plus_m)
        sc.stroke.setStrokeCap(skia.Paint.kRound_Cap)
        sc.stroke.setStrokeWidth(max(0.9, 0.025 * size))
        sc.stroke.setColor(_argb((255, 252, 240), a))
        canvas.drawLine(px, base - 0.88 * size, px, base + 0.16 * size, sc.stroke)
    elif motif == "gust":
        a = a_base * 0.24 * min(1.0, front_u + 0.15)
        if a < 0.01:
            return
        span = width * min(1.0, max(0.15, front_u))
        sc.halo.setBlendMode(plus_m)
        sc.halo.setMaskFilter(_blur_mask(round(0.06 * size, 2)))
        sc.halo.setColor(_argb(gcol, a * 0.45))
        canvas.drawOval(skia.Rect.MakeLTRB(x - 0.08 * size, base - 0.62 * size,
                                           x + span + 0.10 * size, base - 0.14 * size), sc.halo)
        sc.stroke.setBlendMode(plus_m)
        sc.stroke.setStrokeCap(skia.Paint.kRound_Cap)
        sc.stroke.setStrokeWidth(max(1.1, 0.036 * size))
        sc.stroke.setColor(_argb(gcol, a))
        canvas.drawLine(x - 0.08 * size, base - 0.15 * size, x + span, base - 0.34 * size, sc.stroke)
        canvas.drawLine(x + 0.05 * size, base - 0.42 * size, x + span + 0.10 * size, base - 0.60 * size, sc.stroke)
    elif motif == "corona":
        a = a_base * 0.24 * min(1.0, front_u + 0.2)
        if a < 0.01:
            return
        cx, cy_w = x + width * 0.5, base - CENTER_Y * size
        sc.halo.setBlendMode(plus_m)
        sc.halo.setMaskFilter(_blur_mask(round(0.22 * size, 2)))
        sc.halo.setColor(_argb(gcol, a))
        canvas.drawOval(skia.Rect.MakeLTRB(cx - width * 0.58, cy_w - 0.58 * size,
                                           cx + width * 0.58, cy_w + 0.58 * size), sc.halo)
    elif motif == "equinox":
        a = a_base * 0.34
        if a < 0.01:
            return
        cx, y_eq = x + width * 0.5, base + 0.17 * size
        half_w = 0.5 * width * min(1.0, front_u + 0.15)
        sc.stroke.setBlendMode(plus_m)
        sc.stroke.setStrokeCap(skia.Paint.kRound_Cap)
        sc.stroke.setStrokeWidth(max(1.0, 0.032 * size))
        sc.stroke.setColor(_argb(p.color_from, a))
        canvas.drawLine(cx - half_w, y_eq, cx, y_eq, sc.stroke)
        sc.stroke.setColor(_argb(p.color_to, a))
        canvas.drawLine(cx, y_eq, cx + half_w, y_eq, sc.stroke)
        sc.core.setBlendMode(plus_m)
        sc.core.setColor(_argb((255, 250, 240), a * 1.15))
        canvas.drawCircle(cx, y_eq, max(1.0, 0.045 * size), sc.core)
    elif motif == "vigil":
        pulse = 0.85 + 0.15 * math.sin(2.8 * t)
        a = a_base * 0.24 * pulse * min(1.0, front_u + 0.2)
        if a < 0.01:
            return
        cx, y_v = x + width * 0.5, base + 0.10 * size
        sc.halo.setBlendMode(plus_m)
        sc.halo.setMaskFilter(_blur_mask(round(0.18 * size, 2)))
        sc.halo.setColor(_argb(gcol, a))
        canvas.drawOval(skia.Rect.MakeLTRB(cx - width * 0.52, y_v - 0.26 * size,
                                           cx + width * 0.52, y_v + 0.26 * size), sc.halo)


def draw_word_particles(canvas: skia.Canvas, fx: FxWord, letters: list[Letter], x: float, base: float, size: float,
                        t: float, reduce: bool = False, onsets: list[float] | None = None,
                        backdrop: tuple[int, int, int] | None = None, carrier: CarrierWord | None = None,
                        wipe_alpha: float = 1.0) -> None:
    """Render only the particle and sparkle pass for `fx` (used outside text wipe masks)."""
    n = len(letters)
    if reduce or n == 0 or wipe_alpha <= 0.003:
        return
    p = adapt_params(fx.params, backdrop)
    if p.particles <= 0 and p.sparkle <= 0:
        return
    m = fx.match
    b0, b1 = m.begin_ms / 1000.0, m.end_ms / 1000.0
    eff_spacing = max(-0.006, min(0.014, p.spacing_em)) * carrier.fill_front_u if carrier is not None else p.spacing_em
    gap = eff_spacing * size
    c_letters = carrier.letters if (carrier is not None and len(carrier.letters) == n) else None
    c_onsets = [cl.onset_s for cl in c_letters] if c_letters else onsets
    front_u = carrier.fill_front_u if carrier is not None else None
    xs_em = [(L.x + i * gap) / size for i, L in enumerate(letters)]
    ws_em = [L.w / size for L in letters]
    halo = _blur_mask(round(HALO_SIGMA * size, 3))
    dark_factor = _backdrop_dark_factor(backdrop)
    sc = _scratch()
    for q in particles(p, xs_em, ws_em, t, b0, b1, reduce, onsets=c_onsets, fill_front_u=front_u):
        if q.alpha * wipe_alpha < 0.01:
            continue
        _draw_particle(canvas, q, x + q.x_em * size, base + q.y_em * size, q.r_em * size, size,
                       halo, dark_factor, sc, alpha_scale=wipe_alpha)


def draw_word_fx(canvas: skia.Canvas, fonts, fx: FxWord, letters: list[Letter], x: float, base: float, size: float,
                 t: float, rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onsets: list[float] | None = None,
                 backdrop: tuple[int, int, int] | None = None, carrier: CarrierWord | None = None,
                 origin_y: float | None = None, wipe_alpha: float = 1.0, include_particles: bool = True) -> None:
    """`onsets`: segundo en que se canta cada letra (según las sílabas); las letras entran y destellan a ese ritmo.
    `backdrop`: color medio del fondo detrás de la palabra; el color se corrige para que contraste con él (sin cambiar el tono)."""
    n = len(letters)
    if n == 0 or wipe_alpha <= 0.003:
        return
    p, m = adapt_params(fx.params, backdrop), fx.match
    b0, b1 = m.begin_ms / 1000.0, m.end_ms / 1000.0
    eff_spacing = max(-0.006, min(0.014, p.spacing_em)) * carrier.fill_front_u if carrier is not None else p.spacing_em
    gap = eff_spacing * size
    xs = [x + L.x + i * gap for i, L in enumerate(letters)]  # x absoluta de cada letra con el espaciado aplicado
    width = letters[-1].x + letters[-1].w + (n - 1) * gap
    blobs = _word_blobs(fonts, "".join(L.ch for L in letters), size)
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
        ghost_paint = skia.Paint(AntiAlias=True)
        for delay, a0 in GHOSTS:
            for i in range(n):
                if c_letters is not None and c_letters[i].fill_p <= 0.0 and p.erode <= 0.0:
                    continue
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
                ghost_paint.setColor(_argb(g.color, a))
                canvas.drawTextBlob(blobs[i], xs[i], base, ghost_paint)
                canvas.restore()

    go = smoothstep(b0, b0 + 0.12, t)  # los ecos no se mueven hasta que se canta la palabra
    echo_paint = skia.Paint(AntiAlias=True) if p.trail > 0.0 else None
    for dx, dy, a0 in (echoes(p, t, reduce) if p.trail > 0.0 else ()):  # ecos persistentes (turbulencia lenta del tiempo)
        dx, dy = dx * go, dy * go
        for i, st in enumerate(states):
            a = st.alpha * a0
            if a < 0.004:
                continue
            _enter(canvas, st, xs[i] + letters[i].w / 2, cy, size, dx_extra=dx, dy_extra=dy)
            echo_paint.setColor(_argb(st.color, a))
            canvas.drawTextBlob(blobs[i], xs[i], base, echo_paint)
            canvas.restore()

    dark_factor = _backdrop_dark_factor(backdrop)
    _draw_motif(canvas, p, x, base, width, size, t, b0, b1, dark_factor, carrier, wipe_alpha, reduce)
    if any(s.glow > 0.02 for s in states):
        # Draw cached white blurred letter sprites tinted in-place with ColorFilter to avoid per-word FBO saveLayer stalls.
        tier = _glow_tier(p.glow_spread)
        glow_paint = skia.Paint()
        add_inner = dark_factor > 0.05 and p.glow_spread >= 0.45 and not reduce
        plus_paint = skia.Paint(BlendMode=skia.BlendMode.kPlus) if add_inner else None
        for i, st in enumerate(states):
            a = min(1.0, st.glow * 0.7) * st.alpha * wipe_alpha
            if a < 0.01:
                continue
            gcol = p.glow_color if p.glow_color is not None else st.color
            img, pad_g = _glow_image(fonts, letters[i].ch, size, letters[i].w, tier)
            _enter(canvas, st, xs[i] + letters[i].w / 2, cy, size)
            glow_paint.setAlphaf(a)
            glow_paint.setColorFilter(_tint_cf(_argb(gcol, 1.0)))
            canvas.drawImage(img, xs[i] - pad_g, base - GLOW_ASC * size - pad_g, _SAMP_LINEAR, glow_paint)
            if plus_paint is not None and st.glow > 0.25:
                a_plus = min(0.45, a * 0.42 * dark_factor * p.glow_spread)
                if a_plus >= 0.01:
                    inner_col = (255, 224, 150) if p.particle_kind == "ember" else gcol
                    img0, pad0 = _glow_image(fonts, letters[i].ch, size, letters[i].w, 0)
                    plus_paint.setAlphaf(a_plus)
                    plus_paint.setColorFilter(_tint_cf(_argb(inner_col, 1.0)))
                    canvas.drawImage(img0, xs[i] - pad0, base - GLOW_ASC * size - pad0, _SAMP_LINEAR, plus_paint)
            canvas.restore()

    # el desenfoque es común a todas las letras: una capa de palabra (el alpha de cada letra se dibuja dentro)
    sigma = states[0].blur_em * size * 0.5
    wblur = sigma * 2 > BLUR_MIN_PX
    if wblur:
        canvas.saveLayer(box, skia.Paint(ImageFilter=_word_blur_filter(round(sigma, 2))))
    body_paint = skia.Paint(AntiAlias=True)
    for i, st in enumerate(states):
        eff_alpha = st.alpha * wipe_alpha
        if eff_alpha < 0.004:
            continue
        L = letters[i]
        _enter(canvas, st, xs[i] + L.w / 2, cy, size)
        flip = 1.0 if p.seed >> i & 1 else -1.0  # sentido de giro de cada letra al partirse, estable por semilla
        slant = (((p.seed >> (i + 3)) & 7) - 3.5) * 0.04  # inclinación del corte en em
        chroma_i = chroma * c_letters[i].fill_p if c_letters is not None else chroma
        if abs(chroma_i) >= CHROMA_MIN_PX:
            e = 0.4 * size + abs(chroma_i)
            canvas.saveLayer(skia.Rect.MakeLTRB(xs[i] - e, base - 1.3 * size - e, xs[i] + L.w + e, base + 0.5 * size + e),
                             skia.Paint(Alphaf=eff_alpha))
            _body(canvas, blobs[i], xs[i], base, size, L.w, st.color, fr, chroma_i, flip, slant)
            canvas.restore()
        elif fr >= 0.01:  # las mitades se solapan en el borde: alpha por capa para no doblarlo
            canvas.saveLayer(None, skia.Paint(Alphaf=eff_alpha))
            _body(canvas, blobs[i], xs[i], base, size, L.w, st.color, fr, 0.0, flip, slant)
            canvas.restore()
        else:
            body_paint.setColor(_argb(st.color, eff_alpha))
            canvas.drawTextBlob(blobs[i], xs[i], base, body_paint)
        canvas.restore()
    if wblur:
        canvas.restore()

    if include_particles and (p.particles > 0 or p.sparkle > 0):
        halo = _blur_mask(round(HALO_SIGMA * size, 3))
        c_onsets = [cl.onset_s for cl in c_letters] if c_letters else onsets
        front_u = carrier.fill_front_u if carrier is not None else None
        sc = _scratch()
        for q in particles(p, [(xi - x) / size for xi in xs], [L.w / size for L in letters], t, b0, b1, reduce,
                           onsets=c_onsets, fill_front_u=front_u):
            if q.alpha < 0.01:
                continue
            _draw_particle(canvas, q, x + q.x_em * size, base + q.y_em * size, q.r_em * size, size,
                           halo, dark_factor, sc)

    if grain:
        frame = int(t * 24) if t >= 0 else -int(-t * 24) - 1
        k = (p.seed ^ (frame * 0x9E3779B1)) & 0xFFFFFFFF
        tile = _grain_tile(k % GRAIN_VARIANTS)
        off = (k >> 8) % GRAIN_TILE  # desplazamiento extra para que dos fotogramas con la misma tesela difieran
        shader = tile.makeShader(skia.TileMode.kRepeat, skia.TileMode.kRepeat, _SAMP_DEFAULT,
                                 skia.Matrix.Translate(off, (k >> 16) % GRAIN_TILE))
        canvas.drawRect(box, skia.Paint(
            Shader=shader, Alphaf=min(1.0, p.grain * 0.35), BlendMode=skia.BlendMode.kSrcATop))
        canvas.restore()
