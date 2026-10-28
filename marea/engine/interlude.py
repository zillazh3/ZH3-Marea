"""Interludio de tres puntos (AMLL interlude-dots), en ms desde el inicio del hueco."""
import math
from dataclasses import dataclass

from marea.engine.curves import bezier, clamp01

LIGHTING = bezier(0.56, 0.01, 0.45, 1)
ENTER_FADE = bezier(0.59, 0.02, 0.07, 1)
EXIT_P1 = bezier(0.14, 0.06, 0.25, 1)
EXIT_P2 = bezier(0.29, 0.03, 1, 0.38)
EXIT_FADE = bezier(0.43, 0.08, 0.83, 0.31)

HIDDEN = (0.0, 1.0, (0.0, 0.0, 0.0), "oculto", (0.0, 0.0, 0.0))


@dataclass
class Plan:
    total: float
    delay_end: float
    body: float
    body_end: float
    mode: str  # "hold" | "breathe"
    dot3_target: float
    period: float = 0.0
    seg: float = 0.0
    dot3_dur: float = 0.0


def plan_interlude(total_ms, delay_end):
    body = total_ms - delay_end - 1000
    if body < 2 * 80 + 750:
        return None
    if body < 3000:
        return Plan(total_ms, delay_end, body, delay_end + body, "hold", 1.0)
    cycles = max(1, math.floor(body / 4000))
    seg = math.floor((body + 750) / 3 + 0.5)  # Math.round
    dot3_dur = body - seg * 2
    return Plan(total_ms, delay_end, body, delay_end + body, "breathe", dot3_dur / seg, body / cycles, seg, dot3_dur)


def breathing(t):
    a = 4 * math.pi * t
    s, c = math.sin(a), math.cos(a)
    return t - 0.084 * s + 0.008 * (1 - c) + 0.0046 * s * (c - s)


def dot_op(f):
    return 0.2 + 0.7 * clamp01(f)


def dot_enter(i, ms):
    x = clamp01((ms - i * 80) / 750)
    return x * x


def _frac(start, dur, ms, target):
    return 0.0 if ms <= start else LIGHTING(clamp01((ms - start) / dur)) * target


def interlude_snap(p, el):
    """-> (opacidad del grupo, escala del grupo, opacidad por punto, fase, relleno por punto)."""
    if p is None or el >= p.total or el < 0:
        return HIDDEN
    if el < p.delay_end:
        return (0.0, 1.0, (0.0, 0.0, 0.0), "espera", (0.0, 0.0, 0.0))
    ms = el - p.delay_end
    enter_op = ENTER_FADE(clamp01(ms / 180))

    def dots(f):
        return (tuple(dot_op(v) * dot_enter(i, ms) for i, v in enumerate(f)),
                tuple(clamp01(v) * dot_enter(i, ms) for i, v in enumerate(f)))

    if el >= p.body_end:
        x = el - p.body_end
        fade_t = clamp01((x - 750) / 250)
        sc = 1 + EXIT_P1(x / 750) * 0.25 if x < 750 else 1.25 - EXIT_P2(clamp01((x - 750) / 250)) * 0.85
        tr = clamp01(x / 750)
        d, f = dots([1, 1, p.dot3_target + (1 - p.dot3_target) * tr])
        return (enter_op * (1 - EXIT_FADE(fade_t)), sc, d, "salida", f)
    if p.mode == "hold":
        d, f = dots([1, 1, 1])
        return (enter_op, 1.0, d, "hold", f)
    pr = breathing((ms % p.period) / p.period)
    sc = 1 + (pr / 0.5) * 0.1 if pr <= 0.5 else 1.1 - ((pr - 0.5) / 0.5) * 0.1
    d, f = dots([_frac(0, p.seg, ms, 1), _frac(p.seg, p.seg, ms, 1), _frac(p.seg * 2, p.dot3_dur, ms, p.dot3_target)])
    return (enter_op, sc, d, "respira", f)
