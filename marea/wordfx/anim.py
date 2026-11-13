"""Matemática pura de los efectos por letra: cada salida es función de (FxParams.seed, índice de letra, t), sin estado ni skia.

Así la preview y el vídeo exportado dan los mismos píxeles y se puede saltar a cualquier t en cualquier orden.
Coordenadas en em (x a la derecha, y hacia abajo); tiempos en segundos de canción.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass

from marea.wordfx.bleed import spring_bleed_arrival
from marea.wordfx.envelope import sample_envelope
from marea.wordfx.fxtypes import RGB, CarrierLetter, FxParams
from marea.wordfx.signatures import CUSTOM_PARTICLE_KINDS, eval_waveform

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
    kind: str  # 'dust' | 'spark' | 'petal' | 'firefly' | 'leaf' | 'snow'
    color: RGB = (255, 255, 255)
    rot_deg: float = 0.0


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
                 rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onset: float | None = None,
                 carrier: CarrierLetter | None = None, line_act: float | None = None,
                 line_end_s: float | None = None, beat_phase: float | None = None) -> LetterState:
    """`onset`: segundo real en que se canta esta letra (de las sílabas de la canción); sin él se reparte a partes iguales.
    When `carrier` is supplied (from Scene), activates on the fill front via the 6-phase spring envelope and
    reshapes the held-note wave using the category's continuous motion signature.
    """
    dur = max(end_s - begin_s, 0.05)
    end_s = begin_s + dur
    dx = dy = rot = 0.0
    on = (begin_s + dur * i / n if onset is None else onset) if carrier is None else carrier.onset_s
    scale = 1.0 if carrier is None else carrier.scale
    alpha = 1.0
    ow = _osc(p, t, reduce)
    sg = strain_boost(p, strain)

    env = None
    if carrier is not None:
        env = sample_envelope(
            t, begin_s, end_s,
            letter_on_t=on,
            is_hero=p.is_hero,
            release_s=p.release_s,
            residue_level=p.residue_level,
            line_end_s=line_end_s,
            line_act=line_act,
            fade_s=p.handoff_overlap_s,
            zeta=p.zeta,
            reduce=reduce,
        )
        # Gate motion/tint smoothly with the fill front passing this letter (anticipation allowed for hero)
        fill_gate = max(carrier.fill_p, 0.13 if (p.is_hero and env.phase == "anticipation") else 0.0)
        dy += carrier.y_em

    if not reduce:
        if carrier is not None and env is not None:
            # Keep every unsung letter locked on the Apple Music baseline: geometry only moves once fill_p > 0.
            fp = carrier.fill_p
            go = min(1.0, env.motion_amp) * fp
            if p.drift_em != 0.0:
                cap_drift = math.copysign(min(abs(p.drift_em), 0.04), p.drift_em)
                dy += cap_drift * go
            hz = p.jitter_hz if p.jitter_hz > 0.0 else 6.0 if p.osc_jitter_em > 0.0 else 0.0
            amp = p.jitter_em + (p.osc_jitter_em * ow if p.osc_hz > 0.0 else 0.0)
            if p.sig_amp_em > 0.0:
                amp *= 0.45  # avoid stacking full jitter + signature oscillation (e.g. exaggerated wobble)
            if sg > 0.0:  # vocal strain micro-tremor
                amp = amp * (1.0 + 1.2 * sg) if amp > 0.0 else 0.010 * sg
                hz = hz or 10.0
            if amp > 0.0 and hz > 0.0:
                amp *= go * min(1.6, max(0.3, 1.0 + p.audio_gain * (rms - 0.5) * 2.0))
                dx += amp * noise1(_key(p, i, 1), t * hz)
                dy += amp * noise1(_key(p, i, 2), t * hz)
            if p.sig_amp_em > 0.0 and fp > 0.0:
                # env.motion_amp already encodes held_factor / accent scaling; gate strictly by fill front fp
                mode_gain = 1.0 if env.mode == "held" else 0.65
                sig_scale = env.motion_amp * mode_gain * fp
                w_val = eval_waveform(p.waveform, t, p.sig_freq_hz, i, hash01(p.seed, 77), beat_phase=beat_phase)
                s_disp = p.sig_amp_em * sig_scale * w_val
                if p.sig_axis == "y":
                    dy += s_disp
                elif p.sig_axis == "x":
                    dx += s_disp
                elif p.sig_axis == "scale":
                    scale += s_disp * 0.7
                elif p.sig_axis == "rot":
                    rot += s_disp * 35.0
            elif p.wave_em > 0.0 and fp > 0.0:
                dy += go * min(p.wave_em, 0.035) * math.sin(2.0 * math.pi * p.wave_hz * t - 0.9 * i)
            if p.scale_amp > 0.0 and p.scale_period_s > 0.0 and t >= begin_s and fp > 0.0:
                scale += min(p.scale_amp, 0.06) * fp * min(1.0, env.motion_amp) * math.sin(math.pi * (t - begin_s) / p.scale_period_s) ** 2
            if p.rotation_deg > 0.0 and fp > 0.0:
                rot += (hash01(_key(p, i, 3)) * 2.0 - 1.0) * min(p.rotation_deg, 4.0) * go
            # Clamp semantic offset over carrier baseline to keep line alignment clean
            sem_dy = dy - carrier.y_em
            max_off = 0.08 if p.is_hero and env.mode == "held" else 0.05
            dy = carrier.y_em + max(-max_off, min(max_off, sem_dy))
            dx = max(-max_off, min(max_off, dx))
        else:
            go = smoothstep(begin_s, begin_s + 0.12, t)
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
                r_env = smoothstep(begin_s, begin_s + 0.2, t) * (1.0 - smoothstep(end_s, end_s + 0.4, t))
                rot += (hash01(_key(p, i, 3)) * 2.0 - 1.0) * p.rotation_deg * r_env
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
    if carrier is not None:
        glow = min(1.0, max(glow * min(1.0, env.motion_amp), carrier.glow_a))
    if sg > 0.0:
        glow = min(1.0, glow + 0.25 * sg)
    if glow > 0.0 and not reduce:  # mini destello: el bloom de cada letra sube un instante justo al empezar y vuelve a su nivel
        glow = min(1.0, glow + p.flash * smoothstep(on - FLASH_IN_S, on, t) * (1.0 - smoothstep(on, on + FLASH_OUT_S, t)))

    if carrier is not None and p.bleed_donor_s >= 0.0 and p.color_to != p.color_from and not reduce:
        b_gain = spring_bleed_arrival(t, p.bleed_donor_s, p.bleed_donor_end_s if p.bleed_donor_end_s > p.bleed_donor_s else p.bleed_donor_s + 0.6)
        c_end = _lerp(p.color_from, p.color_to, b_gain)
        u = i / (n - 1) if n > 1 else 0.0
        base_col = _lerp(p.color_from, c_end, u)
    else:
        base_col = _color(p, i, n)
    target_col = _lerp(base_col, p.osc_color, ow) if p.osc_hz > 0.0 else base_col
    if carrier is not None and env is not None:
        # Carrier fill tint: mix(base white, category color, fill_tint * env.tint_amp) gated by fill front + anticipation
        tint_w = min(0.60, max(0.0, p.fill_tint * min(1.0, env.tint_amp) * max(carrier.fill_p, 0.25 if env.phase in ("anticipation", "fade") else 0.0)))
        col = _lerp((255, 255, 255), target_col, tint_w)
    else:
        col = target_col
    return LetterState(dx, dy, scale, rot, min(1.0, max(0.0, alpha)), col, blur, glow)


def _custom_particle(p: FxParams, k: int, letters_x_em: Sequence[float], letters_w_em: Sequence[float],
                     t: float, begin_s: float, dur: float, onsets: Sequence[float] | None) -> Particle | None:
    """Emit a themed particle across the active word for seasonal and elemental signatures."""
    n = len(letters_x_em)
    h = lambda s: hash01(p.seed, 300, k, s)  # noqa: E731
    li = int(h(2) * n) % n
    start_s = onsets[li] if (onsets is not None and len(onsets) == n) else begin_s
    spawn = start_s + h(0) * max(0.30, dur * 0.85)
    kind = p.particle_kind
    life = (0.9 + 0.5 * h(1)) if kind in ("ember", "shard") else (1.5 + 0.7 * h(1))
    age = t - spawn
    if not 0.0 <= age < life:
        return None
    x0 = letters_x_em[li] + h(3) * letters_w_em[li]
    ph = 2.0 * math.pi * h(10)
    col = _color(p, li, n)
    env = math.sin(math.pi * min(1.0, age / life)) ** 0.75
    if kind == "petal":
        x = x0 + (0.16 + 0.16 * h(4)) * age + 0.13 * math.sin(2.3 * age + ph)
        y = -0.15 - 0.55 * h(5) - (0.34 + 0.28 * h(6)) * age + 0.05 * math.sin(3.4 * age + ph)
        rot = (h(11) - 0.5) * 90.0 + (70.0 if h(12) > 0.5 else -70.0) * age
        col = _lerp(col, (255, 210, 226) if h(8) < 0.65 else (255, 255, 255), 0.35)
        return Particle(x, y, 0.065 + 0.050 * h(7), 0.92 * env, "petal", col, rot_deg=rot)
    if kind == "firefly":
        x = x0 + 0.18 * math.sin(2.1 * age + ph) + 0.08 * (h(4) - 0.5) * age
        y = -0.25 - 0.65 * h(5) - (0.16 + 0.14 * h(6)) * age + 0.14 * math.cos(1.7 * age + ph * 0.7)
        blink = 0.30 + 0.70 * (math.sin(math.pi * ((1.2 + 0.9 * h(9)) * age + h(11))) ** 2)
        return Particle(x, y, 0.045 + 0.045 * h(7), 0.98 * env * blink, "firefly", _lerp(col, (255, 232, 145), 0.45))
    if kind == "leaf":
        x = x0 + 0.14 * (h(4) - 0.35) * age + 0.22 * math.sin(3.1 * age + ph)
        y = -0.60 - 0.45 * h(5) + (0.30 + 0.24 * h(6)) * age + 0.08 * age * age
        rot = 52.0 * math.cos(3.1 * age + ph) + (h(11) - 0.5) * 35.0
        col = _lerp(col, (242, 168, 72) if h(8) < 0.5 else (214, 98, 62), 0.40)
        return Particle(x, y, 0.070 + 0.050 * h(7), 0.94 * env, "leaf", col, rot_deg=rot)
    if kind == "snow":
        x = x0 + 0.09 * (h(4) - 0.25) * age + 0.08 * math.sin(1.9 * age + ph)
        y = -0.80 - 0.45 * h(5) + (0.24 + 0.18 * h(6)) * age
        rot = 360.0 * h(11) + (42.0 if h(12) > 0.5 else -42.0) * age
        return Particle(x, y, 0.060 + 0.055 * h(7), 0.92 * env, "snow", _lerp(col, (235, 249, 255), 0.60), rot_deg=rot)
    if kind == "ember":
        x = x0 + (h(4) - 0.5) * 0.26 * age + 0.11 * math.sin(6.5 * age + ph)
        y = -0.10 - 0.45 * h(5) - (0.65 + 0.45 * h(6)) * age
        flicker = 0.50 + 0.50 * (math.sin(14.0 * age + ph) ** 2)
        col = _lerp(col, (255, 220, 115) if h(8) < 0.6 else (255, 110, 45), 0.45)
        return Particle(x, y, 0.042 + 0.040 * h(7), 0.96 * (1.0 - age / life) ** 1.1 * flicker, "ember", col, rot_deg=(h(11) - 0.5) * 40.0)
    if kind == "raindrop":
        x = x0 + 0.06 * age
        y = -0.55 - 0.40 * h(5) + (0.68 + 0.42 * h(6)) * age
        return Particle(x, y, 0.048 + 0.036 * h(7), 0.88 * env, "raindrop", _lerp(col, (175, 215, 255), 0.45), rot_deg=-6.0)
    if kind == "bubble":
        x = x0 + 0.06 * (h(4) - 0.5) * age + 0.10 * math.sin(3.0 * age + ph)
        y = -0.05 - 0.40 * h(5) - (0.36 + 0.25 * h(6)) * age
        return Particle(x, y, 0.055 + 0.055 * h(7), 0.86 * env, "bubble", _lerp(col, (190, 244, 255), 0.5))
    if kind == "mist":
        x = x0 + 0.18 * (h(4) - 0.4) * age + 0.12 * math.sin(1.2 * age + ph)
        y = -0.25 - 0.55 * h(5) - (0.14 + 0.12 * h(6)) * age
        return Particle(x, y, 0.15 + 0.12 * h(7), 0.46 * env, "mist", _lerp(col, (240, 248, 255), 0.55))
    if kind == "shard":
        vx = (h(4) - 0.5) * 0.70
        x = x0 + vx * age
        y = -0.25 - 0.35 * h(5) + (0.20 * h(6) - 0.15) * age + 0.55 * age * age
        rot = 360.0 * h(11) + (220.0 if h(12) > 0.5 else -220.0) * age
        return Particle(x, y, 0.060 + 0.048 * h(7), 0.94 * (1.0 - age / life), "shard", _lerp(col, (235, 245, 255), 0.55), rot_deg=rot)
    if kind == "wisp":
        ang = ph + (1.6 + 0.8 * h(9)) * age
        rad = 0.16 + 0.20 * h(4)
        x = x0 + math.cos(ang) * rad
        y = -0.45 - 0.35 * h(5) - 0.14 * age + math.sin(ang) * rad * 0.55
        return Particle(x, y, 0.050 + 0.045 * h(7), 0.82 * env, "wisp", _lerp(col, (230, 215, 255), 0.45))
    # seed
    x = x0 + (0.48 + 0.32 * h(4)) * age
    y = -0.25 - 0.50 * h(5) - 0.18 * age + 0.11 * math.sin(2.5 * age + ph)
    return Particle(x, y, 0.062 + 0.045 * h(7), 0.88 * env, "seed", _lerp(col, (240, 250, 255), 0.65), rot_deg=24.0 + 18.0 * math.cos(2.5 * age + ph))


def particles(p: FxParams, letters_x_em: Sequence[float], letters_w_em: Sequence[float], t: float,
              begin_s: float, end_s: float, reduce: bool = False,
              onsets: Sequence[float] | None = None, fill_front_u: float | None = None) -> list[Particle]:
    n = len(letters_x_em)
    dur = max(end_s - begin_s, 0.05)
    if reduce or n == 0 or p.duration_mode == "accent" or dur < 0.25 or (p.particles <= 0 and p.sparkle <= 0):
        return []
    end_s = begin_s + dur
    out: list[Particle] = []

    if p.particle_kind in CUSTOM_PARTICLE_KINDS:
        for k in range(max(p.particles, 0)):
            q = _custom_particle(p, k, letters_x_em, letters_w_em, t, begin_s, dur, onsets)
            if q is not None:
                out.append(q)
    else:
        ne = max(_eroded(p, n), 1)  # sin erosión el polvo sale de la última letra
        for k in range(max(p.particles, 0)):
            h = lambda s: hash01(p.seed, 100, k, s)  # noqa: E731
            li = n - ne + int(h(2) * ne) % ne
            if onsets is not None and len(onsets) == n:
                # Fill-front spawn: each particle emits when the fill front crosses its target letter
                spawn = onsets[li] + h(0) * min(0.25, max(0.05, end_s - onsets[li]))
            else:
                spawn = (p.vanish_s + h(0) * VANISH_S if p.vanish_s > 0.0 else end_s - ERODE_TAIL * dur + h(0) * (ERODE_TAIL * dur + DRIFT_TAIL_S))
            life = 1.0 + 0.4 * h(1)
            age = t - spawn
            if not 0.0 <= age < life:
                continue
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
        front_x = (fill_front_u * width) if fill_front_u is not None else None
        for k in range(p.sparkle):
            h = lambda s: hash01(p.seed, 200, k, s)  # noqa: E731
            tw = max(0.0, math.sin(2.0 * math.pi * ((0.8 + h(0)) * t + h(1)))) ** 2
            u = (h(2) * 1.2 - 0.1)
            if front_x is not None:
                # Sparkles spawn around the live fill front while singing, spreading as word finishes
                x = min(width * 1.05, max(-0.05 * width, front_x + (h(2) - 0.5) * 0.35 * width))
                ci = min(n - 1, max(0, int((x / max(width, 1e-4)) * n)))
            else:
                x = u * width
                ci = min(n - 1, max(0, int(u * n)))
            out.append(Particle(x, -1.0 + 1.25 * h(3), 0.11 + 0.12 * h(4), tw * win, "spark",
                                 _lerp(_color(p, ci, n), (255, 255, 255), 0.5)))
    return out
