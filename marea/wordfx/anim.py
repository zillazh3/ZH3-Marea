"""Matemática pura de los efectos por letra: cada salida es función de (FxParams.seed, índice de letra, t), sin estado ni skia.

Así la preview y el vídeo exportado dan los mismos píxeles y se puede saltar a cualquier t en cualquier orden.
Coordenadas en em (x a la derecha, y hacia abajo); tiempos en segundos de canción.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass

from marea.wordfx.fxtypes import RGB, FxParams

_M = (1 << 64) - 1
ENTER_S = 0.12  # duración del fundido de entrada de cada letra
ERODE_TAIL = 0.4  # fracción final del intervalo cantado en la que se disuelven las letras erosionadas (sin vanish_s)
VANISH_S = 1.0  # con vanish_s, duración de la disolución desde que arranca el verso siguiente
DRIFT_TAIL_S = 0.15  # el drift sigue un poco más allá del final de la palabra
ECHO_HZ = 0.35  # oscilación de los ecos de la estela
GRAVITY = 1.2  # em/s² de las partículas de polvo


@dataclass(frozen=True)
class LetterState:
    dx: float
    dy: float
    scale: float
    rot_deg: float
    alpha: float
    color: RGB
    blur_em: float
    glow: float


@dataclass(frozen=True)
class Particle:
    x_em: float
    y_em: float
    r_em: float
    alpha: float
    kind: str  # 'dust' | 'spark'
    color: RGB = (255, 255, 255)


FLASH_IN_S, FLASH_OUT_S = 0.06, 0.3  # destello de inicio de letra: subida y bajada (s); la fuerza es FxParams.flash


def _mix(z: int) -> int:
    z = (z + 0x9E3779B97F4A7C15) & _M
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M
    return z ^ (z >> 31)


def hash01(seed: int, *keys: int) -> float:
    """Uniforme en [0,1) por mezcla de enteros (splitmix64); no usa hash() ni random."""
    z = _mix(seed & _M)
    for k in keys:
        z = _mix(z ^ (k & _M))
    return (z >> 11) / 9007199254740992.0


def noise1(seed: int, x: float) -> float:
    """Ruido de valor 1-D suave en [-1,1], continuo en x."""
    i = math.floor(x)
    f = x - i
    u = f * f * (3.0 - 2.0 * f)
    a = hash01(seed, i) * 2.0 - 1.0
    b = hash01(seed, i + 1) * 2.0 - 1.0
    return a + (b - a) * u


def smoothstep(a: float, b: float, x: float) -> float:
    if b <= a:
        return 1.0 if x >= b else 0.0
    u = min(1.0, max(0.0, (x - a) / (b - a)))
    return u * u * (3.0 - 2.0 * u)


def _key(p: FxParams, i: int, salt: int) -> int:
    return (p.seed * 0x9E3779B1 + i * 0x85EBCA6B + salt * 0xC2B2AE35) & _M


def _color(p: FxParams, i: int, n: int) -> RGB:
    u = i / (n - 1) if n > 1 else 0.0
    return tuple(round(a + (b - a) * u) for a, b in zip(p.color_from, p.color_to))  # type: ignore[return-value]


def _lerp(a: RGB, b: RGB, u: float) -> RGB:
    return tuple(round(x + (y - x) * u) for x, y in zip(a, b))  # type: ignore[return-value]


def strain_boost(p: FxParams, strain: float) -> float:
    """Tensión vocal efectiva 0..1 (strain_gain * strain); 0 si el efecto está apagado."""
    return p.strain_gain * min(1.0, max(0.0, strain)) if p.strain_gain > 0.0 else 0.0


def _osc(p: FxParams, t: float, reduce: bool) -> float:
    """Peso 0..1 del polo de la pareja; en reduce queda congelado a medio camino."""
    if p.osc_hz <= 0.0 or reduce:
        return 0.5
    return 0.5 + 0.5 * math.sin(2.0 * math.pi * p.osc_hz * t + p.osc_phase)


def echoes(p: FxParams, t: float, reduce: bool = False) -> list[tuple[float, float, float]]:
    """Ecos persistentes de la estela: [(dx_em, dy_em, factor de alpha)]; oscilan lento, quietos en reduce."""
    if p.trail <= 0.0:
        return []
    ph = hash01(p.seed, 300)
    out = []
    for k, a in enumerate((0.2, 0.1)):
        if reduce:
            out.append((0.0, 0.0, a * p.trail))
        else:
            u = 2.0 * math.pi * (ECHO_HZ * t + ph) + k * 1.7
            out.append((0.025 * math.sin(u * 0.8 + 1.0), 0.05 * math.sin(u), a * p.trail))
    return out


def _eroded(p: FxParams, n: int) -> int:
    """Cuántas de las últimas letras se disuelven."""
    if p.erode <= 0.0:
        return 0
    k = round(p.erode * n)
    return min(n, max(k, 1 if p.erode > 0.05 else 0))


def letter_state(p: FxParams, i: int, n: int, t: float, begin_s: float, end_s: float,
                 rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onset: float | None = None) -> LetterState:
    """`onset`: segundo real en que se canta esta letra (de las sílabas de la canción); sin él se reparte a partes iguales."""
    dur = max(end_s - begin_s, 0.05)
    end_s = begin_s + dur
    dx = dy = rot = 0.0
    on = begin_s + dur * i / n if onset is None else onset
    scale = 1.0
    alpha = 1.0
    ow = _osc(p, t, reduce)
    sg = strain_boost(p, strain)

    if not reduce:
        go = smoothstep(begin_s, begin_s + 0.12, t)  # el movimiento continuo arranca cuando la palabra se canta, no antes
        dy += p.drift_em * smoothstep(begin_s, end_s + DRIFT_TAIL_S, t)
        hz = p.jitter_hz if p.jitter_hz > 0.0 else 6.0 if p.osc_jitter_em > 0.0 else 0.0
        amp = p.jitter_em + (p.osc_jitter_em * ow if p.osc_hz > 0.0 else 0.0)
        if sg > 0.0:  # tensión vocal: más temblor; sin jitter propio aparece uno mínimo a ~12 Hz
            amp = amp * (1.0 + 1.5 * sg) if amp > 0.0 else 0.012 * sg
            hz = hz or 12.0
        if amp > 0.0 and hz > 0.0:
            amp *= go * min(2.0, max(0.3, 1.0 + p.audio_gain * (rms - 0.5) * 2.0))
            dx += amp * noise1(_key(p, i, 1), t * hz)
            dy += amp * noise1(_key(p, i, 2), t * hz)
        if p.wave_em > 0.0:
            dy += go * p.wave_em * math.sin(2.0 * math.pi * p.wave_hz * t - 0.9 * i)
        if p.scale_amp > 0.0 and p.scale_period_s > 0.0 and t >= begin_s:
            scale += p.scale_amp * math.sin(math.pi * (t - begin_s) / p.scale_period_s) ** 2
        if p.rotation_deg > 0.0:
            env = smoothstep(begin_s, begin_s + 0.2, t) * (1.0 - smoothstep(end_s, end_s + 0.4, t))
            rot = (hash01(_key(p, i, 3)) * 2.0 - 1.0) * p.rotation_deg * env
        if p.stagger_ms > 0.0 or p.letter_offset_em > 0.0:
            t0 = begin_s + i * p.stagger_ms / 1000.0 if onset is None else onset  # con sílabas reales, la letra entra cuando se canta
            k = smoothstep(t0, t0 + ENTER_S, t)
            alpha *= k
            dy += p.letter_offset_em * (1.0 - k)

    if p.opacity_min < 1.0:
        phase = hash01(_key(p, 0, 4))  # una fase por palabra: las letras respiran juntas
        osc = 0.5 + 0.5 * math.sin(2.0 * math.pi * (t * 0.7 + phase))
        sung = smoothstep(begin_s, begin_s + dur / 2, t) * (1.0 - smoothstep(begin_s + dur / 2, end_s, t))
        alpha *= 1.0 - (1.0 - max(p.opacity_min, 0.0)) * (0.5 * osc + 0.5 * sung)

    ne = _eroded(p, n)
    if ne and i >= n - ne:
        j = i - (n - ne)
        w0, wl = (p.vanish_s, VANISH_S) if p.vanish_s > 0.0 else (end_s - ERODE_TAIL * dur, ERODE_TAIL * dur)  # ventana de erosión
        s0 = w0 + (j / ne) * 0.4 * wl  # cada letra empieza algo más tarde que la anterior
        e = smoothstep(s0, s0 + 0.6 * wl, t)  # la última termina justo en end_s
        alpha *= 1.0 - e
        scale *= 1.0 - 0.15 * e  # se deshace: encoge y se va con el polvo
        if not reduce:
            dx += (hash01(_key(p, i, 5)) * 2.0 - 1.0) * 0.04 * e
            dy += (0.02 if p.drift_em >= 0.0 else -0.02) * e * (0.5 + hash01(_key(p, i, 6)))

    blur = p.blur_em * (1.0 - smoothstep(begin_s, end_s, t)) if p.blur_em > 0.0 else 0.0
    glow = min(1.0, max(0.0, p.glow * (1.0 + p.audio_gain * (rms - 0.5)))) if p.glow > 0.0 else 0.0
    if sg > 0.0:
        glow = min(1.0, glow + 0.25 * sg)
    if glow > 0.0 and not reduce:  # mini destello: el bloom de cada letra sube un instante justo al empezar y vuelve a su nivel
        glow = min(1.0, glow + p.flash * smoothstep(on - FLASH_IN_S, on, t) * (1.0 - smoothstep(on, on + FLASH_OUT_S, t)))
    return LetterState(dx, dy, scale, rot, min(1.0, max(0.0, alpha)),
                       _lerp(_color(p, i, n), p.osc_color, ow) if p.osc_hz > 0.0 else _color(p, i, n), blur, glow)


def particles(p: FxParams, letters_x_em: Sequence[float], letters_w_em: Sequence[float], t: float,
              begin_s: float, end_s: float, reduce: bool = False) -> list[Particle]:
    n = len(letters_x_em)
    if reduce or n == 0 or (p.particles <= 0 and p.sparkle <= 0):
        return []
    dur = max(end_s - begin_s, 0.05)
    end_s = begin_s + dur
    out: list[Particle] = []

    ne = max(_eroded(p, n), 1)  # sin erosión el polvo sale de la última letra
    for k in range(max(p.particles, 0)):
        h = lambda s: hash01(p.seed, 100, k, s)  # noqa: E731
        spawn = (p.vanish_s + h(0) * VANISH_S if p.vanish_s > 0.0 else end_s - ERODE_TAIL * dur + h(0) * (ERODE_TAIL * dur + DRIFT_TAIL_S))
        life = 1.0 + 0.4 * h(1)
        age = t - spawn
        if not 0.0 <= age < life:
            continue
        li = n - ne + int(h(2) * ne) % ne
        x = (letters_x_em[li] + h(3) * letters_w_em[li] + (h(4) - 0.5) * 0.3 * age
             + 0.04 * math.sin(2.0 * math.pi * ((0.5 + 0.4 * h(9)) * age + h(10))))  # vaivén lateral
        y0 = p.drift_em - 0.1 - 0.5 * h(5)  # las letras erosionadas ya bajaron/subieron con el drift
        if p.drift_em >= 0.0:
            y = y0 - 0.1 * h(6) * age + 0.5 * GRAVITY * age * age
        else:
            y = y0 - (0.2 + 0.2 * h(6)) * age
        col = _color(p, li, n)
        if h(8) < 0.25:  # una de cada cuatro, más clara
            col = _lerp(col, (255, 255, 255), 0.4)
        out.append(Particle(x, y, 0.035 + 0.055 * h(7), 0.95 * (1.0 - age / life) ** 1.5, "dust", col))

    if p.sparkle > 0 and begin_s - 0.1 <= t <= end_s + 3.0:  # se quedan un rato: la palabra brilla mientras su verso sigue activo
        win = smoothstep(begin_s - 0.1, begin_s + 0.2, t) * (1.0 - smoothstep(end_s + 2.5, end_s + 3.0, t))
        width = max(x + w for x, w in zip(letters_x_em, letters_w_em))
        for k in range(p.sparkle):
            h = lambda s: hash01(p.seed, 200, k, s)  # noqa: E731
            tw = max(0.0, math.sin(2.0 * math.pi * ((0.8 + h(0)) * t + h(1)))) ** 2
            u = (h(2) * 1.2 - 0.1)
            x = u * width
            ci = min(n - 1, max(0, int(u * n)))
            out.append(Particle(x, -1.0 + 1.25 * h(3), 0.11 + 0.12 * h(4), tw * win, "spark",
                                 _lerp(_color(p, ci, n), (255, 255, 255), 0.5)))
    return out
