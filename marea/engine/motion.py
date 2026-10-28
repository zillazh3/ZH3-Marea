"""Movimiento por carácter encima de la animación por sílaba: ola, rebote, respiración y entrada de línea.

Todo es función pura de `t` (sin estado): pausas, seeks y retrocesos salen bien sin invalidar nada. Los efectos no se apilan como
animaciones independientes: cada perfil es UN kernel (cresta + cola) y los canales dy/ds se acoplan, y al final un limitador suave
impide que la suma con lo que ya hace la sílaba (notas sostenidas, saltos) pase de LIFT_CAP / SCALE_CAP.
Los tiempos por carácter son una distribución estimada por conteo dentro de la sílaba (no sincronía fonética).
Unidades: dy en em (negativo = sube), ds en escala (se suma a 1.0).
"""
import math
import statistics
import zlib
from dataclasses import dataclass

from marea.engine.curves import WIPE_INV, smoothstep

LIFT_CAP, SCALE_CAP = 0.09, 0.08  # tope de la suma sílaba + capa (em / escala sobre 1.0)
PRE, POST = 0.35, 0.65  # reparto de la ventana de la ola antes / después de la cresta
SPREAD, D_MIN, D_MAX = 4.0, 0.16, 0.6  # ventana = SPREAD × tiempo por carácter, acotada (s)
STEP_MIN, STRETCH_MAX = 0.025, 2.0  # menos de 25 ms entre crestas no se distingue a 60 fps: se abren hasta 2× alrededor del centro
SLOW_S, FAST_S = 0.025, 0.09  # s por carácter: amplitud mínima (A_FLOOR) por debajo de SLOW_S, completa desde FAST_S
A_FLOOR = 0.4
TAIL, TAIL_DECAY, TAIL_PERIOD, FALL_SETTLE = 0.35, 0.12, 0.22, 0.14  # rebote: cola elástica, su amortiguación, periodo y caída previa (s)
BREATH_HZ, BREATH_ATTACK, BREATH_RELEASE = 1.3, 0.25, 0.2
ENTER_DUR, ENTER_STEP, ENTER_MAX_DELAY = 0.2, 0.035, 0.2  # la entrada de toda la línea acaba en ≤ 0.4 s
PRE_MARGIN, POST_MARGIN = 0.4, 0.7  # fuera de [b - PRE_MARGIN, e + POST_MARGIN] la ola vale 0 en cualquier perfil
FAST_LINE, SUSTAIN_S = 0.045, 0.9  # pick: s por carácter de una línea «rápida»; duración de una nota «sostenida»


@dataclass(frozen=True)
class Profile:
    lift: float = 0.0  # em que sube la cresta
    swell: float = 0.0  # escala extra en la cresta
    settle: float = 0.0  # rebote elástico tras la cresta (fracción de la cresta)
    breath: float = 0.0  # pulso de escala en notas sostenidas
    enter: float = 0.0  # em que se hunde cada palabra al activarse la línea


PROFILES = {
    "onda": Profile(lift=0.045, swell=0.04, breath=0.012, enter=0.03),
    "rebote": Profile(lift=0.05, swell=0.035, settle=0.5, breath=0.012, enter=0.04),
    "respira": Profile(lift=0.03, swell=0.02, breath=0.03, enter=0.02),
    "suave": Profile(lift=0.025, swell=0.015, enter=0.015),  # letra por línea: tiempos repartidos, solo un levantamiento leve
}
MODES = ("auto", *PROFILES, "clasico")


def pick(line) -> str:
    """Perfil automático de una línea: depende solo de sus tiempos y su texto, así que es reproducible (los coros repetidos se
    mueven igual; crc32 y no hash(), que cambia entre corridas)."""
    if line.approx:
        return "suave"
    spacing = [(s.end - s.begin) / 1000 / len(s.text) for s in line.syllables if s.text.strip()]
    if spacing and statistics.median(spacing) < FAST_LINE:
        return "onda"  # el rebote en tempo rápido se ve nervioso
    if any((s.end - s.begin) / 1000 >= SUSTAIN_S for s in line.syllables):
        return "respira"
    return ("onda", "rebote")[zlib.crc32(line.text.encode()) % 2]


def resolve(mode: str, reduce: bool, auto: str) -> Profile | None:
    """Perfil a aplicar según los ajustes; None = sin capa (movimiento reducido o «clásico»)."""
    return None if reduce or mode == "clasico" else PROFILES.get(auto if mode == "auto" else mode)


def limit(wave: float, base: float, cap: float) -> float:
    """`wave` comprimido (tanh) para que base + wave no pase de cap en su dirección; 0 si base ya está al tope."""
    room = cap - (abs(base) if (base > 0) == (wave > 0) else 0.0)
    return math.copysign(room * math.tanh(abs(wave) / room), wave) if wave and room > 1e-9 else 0.0


def bump(u: float, d: float, settle: float) -> float:
    """Forma de la ola con u = t - tiempo de la cresta: sube en PRE·d y baja en POST·d (ambos con tangente 0). Con settle la
    caída es corta y pasa por debajo de la base: cola elástica amortiguada que cierra exacta en 0. Soporte acotado."""
    fall = min(POST * d, FALL_SETTLE) if settle else POST * d
    if u <= -PRE * d or u >= fall + (TAIL if settle else 0.0):
        return 0.0
    if u < 0:
        return smoothstep(1 + u / (PRE * d))
    v = smoothstep(1 - u / fall)
    if settle and u > fall:
        tau = u - fall
        v -= settle * math.exp(-tau / TAIL_DECAY) * math.sin(2 * math.pi * tau / TAIL_PERIOD) * smoothstep(tau / 0.04) * smoothstep((TAIL - tau) / 0.1)
    return v


def enter_dip(pf: Profile, amt: float, t: float, ta: float, word: int) -> float:
    """Hundimiento (em, +) de la palabra `word` de la línea que se activa en `ta`; en cascada, 0 antes y después."""
    x = (t - ta - min(word * ENTER_STEP, ENTER_MAX_DELAY)) / ENTER_DUR
    return pf.enter * amt * math.sin(math.pi * x) ** 2 if 0 < x < 1 else 0.0


def syllable(pf: Profile, amt: float, t: float, b: float, e: float, n: int, sus: float, dip: float,
             base_y: float, base_s: float):
    """(dy[n], ds[n]) de los n caracteres de una sílaba que suena en [b, e], o None si todos están en reposo.
    dip: hundimiento de entrada de su palabra; base_y / base_s: lo que ya hace la sílaba (para no pasarse del tope)."""
    if n < 1 or not (b - PRE_MARGIN <= t <= e + POST_MARGIN or dip):
        return None
    spacing = (e - b) / n
    stretch = min(STRETCH_MAX, max(1.0, STEP_MIN / spacing)) if spacing > 0 else STRETCH_MAX
    d = min(D_MAX, max(D_MIN, SPREAD * spacing))
    k = A_FLOOR + (1 - A_FLOOR) * smoothstep((spacing - SLOW_S) / (FAST_S - SLOW_S))  # poco tiempo por letra: menos amplitud
    mid = (b + e) / 2
    dys, dss = [], []
    for j in range(n):
        tc = b + (e - b) * WIPE_INV((j + 0.5) / n)  # la cresta pasa cuando el borde del wipe cruza la letra
        v = bump(t - (mid + (tc - mid) * stretch), d, pf.settle) * k
        dy, ds = -pf.lift * amt * v, pf.swell * amt * v
        if sus > 0 and pf.breath and b <= t:  # respiración: la nota sostenida pulsa y la onda la recorre
            env = smoothstep((t - b) / BREATH_ATTACK) * (smoothstep(1 - (t - e) / BREATH_RELEASE) if t > e else 1.0)
            p = pf.breath * amt * sus * env * math.sin(2 * math.pi * BREATH_HZ * (t - b) - 0.5 * j)
            ds, dy = ds + p, dy - 0.4 * p
        dys.append(limit(dy + dip, base_y, LIFT_CAP))
        dss.append(limit(ds, base_s, SCALE_CAP))
    return (dys, dss) if any(abs(v) > 1e-5 for v in dys + dss) else None
