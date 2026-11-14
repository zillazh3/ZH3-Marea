"""Matemática pura de los efectos por letra: cada salida es función de (FxParams.seed, índice de letra, t), sin estado ni skia.

Así la preview y el vídeo exportado dan los mismos píxeles y se puede saltar a cualquier t en cualquier orden.
Coordenadas en em (x a la derecha, y hacia abajo); tiempos en segundos de canción.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass

from marea.wordfx._math import lerp_rgb as _lerp, smoothstep
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


@dataclass(frozen=True, slots=True)
class LetterState:
    dx: float
    dy: float
    scale: float
    rot_deg: float
    alpha: float
    color: RGB
    blur_em: float
    glow: float


@dataclass(frozen=True, slots=True)
class Particle:
    x_em: float
    y_em: float
    r_em: float
    alpha: float
    kind: str  # 'dust' | 'spark' | 'petal' | 'firefly' | 'leaf' | 'snow' | 'ember' | ...
    color: RGB = (255, 255, 255)
    rot_deg: float = 0.0
    style: str = ""  # sub-variant e.g. celestial | ember | buoyant | bokeh | flare | star
    life_u: float = 0.5  # normalized lifetime [0, 1]
    vx_em: float = 0.0
    vy_em: float = 0.0
    core_color: RGB | None = None


def _seed_k(seed: int, salt: int, k: int) -> int:
    return _mix(_mix(seed & _M) ^ (((salt * 0x9E3779B97F4A7C15) + (k * 0xBF58476D1CE4E5B9)) & _M))


def _u01(z_p: int, s: int) -> float:
    return (_mix(z_p ^ ((s * 0x94D049BB133111EB) & _M)) >> 11) / 9007199254740992.0


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
    z0 = _mix(seed & _M)
    a = (_mix(z0 ^ (i & _M)) >> 11) / 9007199254740992.0 * 2.0 - 1.0
    b = (_mix(z0 ^ ((i + 1) & _M)) >> 11) / 9007199254740992.0 * 2.0 - 1.0
    return a + (b - a) * u


def _key(p: FxParams, i: int, salt: int) -> int:
    return (p.seed * 0x9E3779B1 + i * 0x85EBCA6B + salt * 0xC2B2AE35) & _M


def _color(p: FxParams, i: int, n: int) -> RGB:
    u = i / (n - 1) if n > 1 else 0.0
    return _lerp(p.color_from, p.color_to, u)


def strain_boost(p: FxParams, strain: float) -> float:
    """Tensión vocal efectiva 0..1 (strain_gain * strain); 0 si el efecto está apagado."""
    if p.strain_gain <= 0.0 or strain <= 0.0:
        return 0.0
    return p.strain_gain * (1.0 if strain >= 1.0 else strain)


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


@dataclass(frozen=True, slots=True)
class _WordAnimCtx:
    dur: float
    end_s: float
    ow: float
    sg: float
    c_end: RGB
    opacity_factor: float
    blur: float
    ne: int
    base_glow: float
    sig_seed_u: float


def _word_ctx(p: FxParams, n: int, t: float, begin_s: float, end_s: float,
              rms: float, reduce: bool, strain: float, has_carrier: bool) -> _WordAnimCtx:
    dur = max(end_s - begin_s, 0.05)
    end_s = begin_s + dur
    ow = _osc(p, t, reduce)
    sg = strain_boost(p, strain)
    if has_carrier and p.bleed_donor_s >= 0.0 and p.color_to != p.color_from and not reduce:
        b_gain = spring_bleed_arrival(
            t, p.bleed_donor_s, p.bleed_donor_end_s if p.bleed_donor_end_s > p.bleed_donor_s else p.bleed_donor_s + 0.6
        )
        c_end = _lerp(p.color_from, p.color_to, b_gain)
    else:
        c_end = p.color_to
    if p.opacity_min < 1.0:
        phase = hash01(_key(p, 0, 4))
        osc = 0.5 + 0.5 * math.sin(2.0 * math.pi * (t * 0.7 + phase))
        sung = smoothstep(begin_s, begin_s + dur / 2, t) * (1.0 - smoothstep(begin_s + dur / 2, end_s, t))
        opacity_factor = 1.0 - (1.0 - max(p.opacity_min, 0.0)) * (0.5 * osc + 0.5 * sung)
    else:
        opacity_factor = 1.0
    blur = p.blur_em * (1.0 - smoothstep(begin_s, end_s, t)) if p.blur_em > 0.0 else 0.0
    ne = _eroded(p, n)
    if p.glow > 0.0:
        g0 = p.glow * (1.0 + p.audio_gain * (rms - 0.5))
        base_glow = 0.0 if g0 < 0.0 else (1.0 if g0 > 1.0 else g0)
    else:
        base_glow = 0.0
    sig_seed_u = hash01(p.seed, 77)
    return _WordAnimCtx(dur, end_s, ow, sg, c_end, opacity_factor, blur, ne, base_glow, sig_seed_u)


def _apply_jitter(
    p: FxParams, i: int, t: float, rms: float, ow: float, sg: float, go: float,
    strain_scale: float, strain_min: float, strain_hz: float, max_ag: float, damp_with_sig: bool,
) -> tuple[float, float]:
    hz = p.jitter_hz if p.jitter_hz > 0.0 else (6.0 if p.osc_jitter_em > 0.0 else 0.0)
    amp = p.jitter_em + (p.osc_jitter_em * ow if p.osc_hz > 0.0 else 0.0)
    if damp_with_sig and p.sig_amp_em > 0.0:
        amp *= 0.45
    if sg > 0.0:
        amp = amp * (1.0 + strain_scale * sg) if amp > 0.0 else strain_min * sg
        hz = hz or strain_hz
    if amp > 0.0 and hz > 0.0:
        ag = 1.0 + p.audio_gain * (rms - 0.5) * 2.0
        amp *= go * (0.3 if ag < 0.3 else (max_ag if ag > max_ag else ag))
        return amp * noise1(_key(p, i, 1), t * hz), amp * noise1(_key(p, i, 2), t * hz)
    return 0.0, 0.0


def _letter_state_with_ctx(
    p: FxParams, i: int, n: int, t: float, begin_s: float, ctx: _WordAnimCtx,
    rms: float, reduce: bool, onset: float | None,
    carrier: CarrierLetter | None, line_act: float | None, line_end_s: float | None,
    beat_phase: float | None,
) -> LetterState:
    dur, end_s, ow, sg = ctx.dur, ctx.end_s, ctx.ow, ctx.sg
    dx = dy = rot = 0.0
    on = (begin_s + dur * i / n if onset is None else onset) if carrier is None else carrier.onset_s
    scale = 1.0 if carrier is None else carrier.scale
    alpha = ctx.opacity_factor

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
        dy += carrier.y_em

    if not reduce:
        if carrier is not None and env is not None:
            fp = carrier.fill_p
            if fp > 0.0:
                m_amp = 1.0 if env.motion_amp > 1.0 else env.motion_amp
                go = m_amp * fp
                if p.drift_em != 0.0:
                    dy += math.copysign(0.04 if abs(p.drift_em) > 0.04 else abs(p.drift_em), p.drift_em) * go
                if p.jitter_em > 0.0 or p.osc_jitter_em > 0.0 or sg > 0.0:
                    jdx, jdy = _apply_jitter(p, i, t, rms, ow, sg, go, 1.2, 0.010, 10.0, 1.6, True)
                    dx += jdx
                    dy += jdy
                if p.sig_amp_em > 0.0:
                    mode_gain = 1.0 if env.mode == "held" else 0.65
                    sig_scale = env.motion_amp * mode_gain * fp
                    w_val = eval_waveform(p.waveform, t, p.sig_freq_hz, i, ctx.sig_seed_u, beat_phase=beat_phase)
                    s_disp = p.sig_amp_em * sig_scale * w_val
                    axis = p.sig_axis
                    if axis == "y":
                        dy += s_disp
                    elif axis == "x":
                        dx += s_disp
                    elif axis == "scale":
                        scale += s_disp * 0.7
                    elif axis == "rot":
                        rot += s_disp * 35.0
                elif p.wave_em > 0.0:
                    w_em = 0.035 if p.wave_em > 0.035 else p.wave_em
                    dy += go * w_em * math.sin(2.0 * math.pi * p.wave_hz * t - 0.9 * i)
                if p.scale_amp > 0.0 and p.scale_period_s > 0.0 and t >= begin_s:
                    s_am = 0.06 if p.scale_amp > 0.06 else p.scale_amp
                    scale += s_am * fp * m_amp * math.sin(math.pi * (t - begin_s) / p.scale_period_s) ** 2
                if p.rotation_deg > 0.0:
                    r_deg = 4.0 if p.rotation_deg > 4.0 else p.rotation_deg
                    rot += (hash01(_key(p, i, 3)) * 2.0 - 1.0) * r_deg * go
                sem_dy = dy - carrier.y_em
                max_off = 0.08 if p.is_hero and env.mode == "held" else 0.05
                if sem_dy > max_off:
                    dy = carrier.y_em + max_off
                elif sem_dy < -max_off:
                    dy = carrier.y_em - max_off
                if dx > max_off:
                    dx = max_off
                elif dx < -max_off:
                    dx = -max_off
        else:
            go = smoothstep(begin_s, begin_s + 0.12, t)
            dy += p.drift_em * smoothstep(begin_s, end_s + DRIFT_TAIL_S, t)
            if p.jitter_em > 0.0 or p.osc_jitter_em > 0.0 or sg > 0.0:
                jdx, jdy = _apply_jitter(p, i, t, rms, ow, sg, go, 1.5, 0.012, 12.0, 2.0, False)
                dx += jdx
                dy += jdy
            if p.wave_em > 0.0:
                dy += go * p.wave_em * math.sin(2.0 * math.pi * p.wave_hz * t - 0.9 * i)
            if p.scale_amp > 0.0 and p.scale_period_s > 0.0 and t >= begin_s:
                scale += p.scale_amp * math.sin(math.pi * (t - begin_s) / p.scale_period_s) ** 2
            if p.rotation_deg > 0.0:
                r_env = smoothstep(begin_s, begin_s + 0.2, t) * (1.0 - smoothstep(end_s, end_s + 0.4, t))
                rot += (hash01(_key(p, i, 3)) * 2.0 - 1.0) * p.rotation_deg * r_env
            if p.stagger_ms > 0.0 or p.letter_offset_em > 0.0:
                t0 = begin_s + i * p.stagger_ms / 1000.0 if onset is None else onset
                k = smoothstep(t0, t0 + ENTER_S, t)
                alpha *= k
                dy += p.letter_offset_em * (1.0 - k)

    ne = ctx.ne
    if ne and i >= n - ne:
        j = i - (n - ne)
        w0, wl = (p.vanish_s, VANISH_S) if p.vanish_s > 0.0 else (end_s - ERODE_TAIL * dur, ERODE_TAIL * dur)
        s0 = w0 + (j / ne) * 0.4 * wl
        e = smoothstep(s0, s0 + 0.6 * wl, t)
        alpha *= 1.0 - e
        scale *= 1.0 - 0.15 * e
        if not reduce:
            dx += (hash01(_key(p, i, 5)) * 2.0 - 1.0) * 0.04 * e
            dy += (0.02 if p.drift_em >= 0.0 else -0.02) * e * (0.5 + hash01(_key(p, i, 6)))

    glow = ctx.base_glow
    if carrier is not None:
        ma = glow * (1.0 if env.motion_amp > 1.0 else env.motion_amp)
        g2 = ma if ma > carrier.glow_a else carrier.glow_a
        glow = 1.0 if g2 > 1.0 else g2
    if sg > 0.0:
        glow = min(1.0, glow + 0.25 * sg)
    if glow > 0.0 and not reduce:
        if p.particle_kind == "ember" and (carrier is None or carrier.fill_p > 0.0):
            flk = noise1(_key(p, i, 9), t * 7.5)
            glow = min(1.0, max(0.05, glow * (1.0 + 0.16 * flk)))
        fl = glow + p.flash * smoothstep(on - FLASH_IN_S, on, t) * (1.0 - smoothstep(on, on + FLASH_OUT_S, t))
        glow = 1.0 if fl > 1.0 else fl

    base_col = _lerp(p.color_from, ctx.c_end, i / (n - 1) if n > 1 else 0.0)
    target_col = _lerp(base_col, p.osc_color, ow) if p.osc_hz > 0.0 else base_col
    if carrier is not None and env is not None:
        ta = 1.0 if env.tint_amp > 1.0 else env.tint_amp
        fg = carrier.fill_p
        if fg < 0.25 and env.phase in ("anticipation", "fade"):
            fg = 0.25
        tw = p.fill_tint * ta * fg
        tint_w = 0.0 if tw < 0.0 else (0.60 if tw > 0.60 else tw)
        col = _lerp((255, 255, 255), target_col, tint_w)
    else:
        col = target_col
    a_out = 0.0 if alpha < 0.0 else (1.0 if alpha > 1.0 else alpha)
    return LetterState(dx, dy, scale, rot, a_out, col, ctx.blur, glow)


def letter_state(p: FxParams, i: int, n: int, t: float, begin_s: float, end_s: float,
                 rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onset: float | None = None,
                 carrier: CarrierLetter | None = None, line_act: float | None = None,
                 line_end_s: float | None = None, beat_phase: float | None = None) -> LetterState:
    """`onset`: segundo real en que se canta esta letra (de las sílabas de la canción); sin él se reparte a partes iguales.
    When `carrier` is supplied (from Scene), activates on the fill front via the 6-phase spring envelope and
    reshapes the held-note wave using the category's continuous motion signature.
    """
    ctx = _word_ctx(p, n, t, begin_s, end_s, rms, reduce, strain, carrier is not None)
    return _letter_state_with_ctx(p, i, n, t, begin_s, ctx, rms, reduce, onset, carrier, line_act, line_end_s, beat_phase)


def word_letter_states(
    p: FxParams, n: int, t: float, begin_s: float, end_s: float,
    rms: float = 0.0, reduce: bool = False, strain: float = 0.0, onsets: Sequence[float] | None = None,
    c_letters: Sequence[CarrierLetter] | None = None, line_act: float | None = None,
    line_end_s: float | None = None, beat_phase: float | None = None,
) -> list[LetterState]:
    """Evaluate all `n` letter states for one word, sharing word-invariant calculations across letters."""
    ctx = _word_ctx(p, n, t, begin_s, end_s, rms, reduce, strain, c_letters is not None)
    return [
        _letter_state_with_ctx(
            p, i, n, t, begin_s, ctx, rms, reduce,
            onsets[i] if onsets else None,
            c_letters[i] if c_letters else None,
            line_act, line_end_s, beat_phase,
        )
        for i in range(n)
    ]


def _month_particle(kind: str, k: int, zp: int, p: FxParams, x0: float, age: float, life: float, life_u: float,
                    env: float, ph: float, col: RGB,
                    u4: float, u5: float, u6: float, u7: float, u9: float, u11: float, u12: float) -> Particle:
    """Emit a deterministic monthly particle with distinct symbolic physics."""
    if kind == "prism":
        side = -1.0 if (k % 2 == 0) else 1.0
        x = x0 + side * (0.14 + 0.16 * u4) * age
        y = -0.25 - 0.50 * u5 - (0.20 + 0.15 * u6) * age
        rot = 45.0 * u11 + side * 35.0 * age
        return Particle(x, y, 0.048 + 0.042 * u7, 0.90 * env, "prism",
                        _lerp(col, (215, 245, 255), 0.45), rot_deg=rot, life_u=life_u,
                        core_color=(255, 248, 224))
    if kind == "candle":
        if k % 3 == 0:
            x = x0 + 0.12 * (u4 - 0.5) * age + 0.06 * math.sin(1.2 * age + ph)
            y = -0.22 - 0.45 * u5 - (0.12 + 0.10 * u6) * age
            r = (0.11 + 0.08 * u7) * (0.85 + 0.35 * life_u)
            return Particle(x, y, r, 0.38 * env, "mist", _lerp(col, (255, 214, 232), 0.55), life_u=life_u)
        x = x0 + 0.05 * math.sin(2.4 * age + ph) + 0.04 * (u4 - 0.5) * age
        y = -0.18 - 0.48 * u5 - (0.28 + 0.18 * u6) * age
        pulse = 0.72 + 0.28 * math.sin(4.5 * age + ph)
        return Particle(x, y, 0.044 + 0.036 * u7, 0.92 * env * pulse, "candle",
                        _lerp(col, (255, 206, 194), 0.50), life_u=life_u, core_color=(255, 242, 228))
    if kind == "gale":
        vx = 0.62 + 0.38 * u4
        vy = -0.24 - 0.16 * u6
        x = x0 + vx * age
        y = -0.22 - 0.50 * u5 + vy * age + 0.07 * math.sin(3.6 * age + ph)
        rot = math.degrees(math.atan2(vy, vx))
        return Particle(x, y, 0.050 + 0.040 * u7, 0.90 * env, "gale",
                        _lerp(col, (164, 248, 194), 0.45), rot_deg=rot, life_u=life_u,
                        vx_em=vx, vy_em=vy, core_color=(242, 255, 215))
    if kind == "dew_petal":
        y_top = -0.78 - 0.25 * u5
        if life_u < 0.35:
            u = life_u / 0.35
            x = x0 + 0.04 * u
            y = y_top + 0.48 * u
            return Particle(x, y, 0.044 + 0.032 * u7, 0.88 * smoothstep(0.0, 0.20, u), "raindrop",
                            _lerp(col, (186, 232, 255), 0.55), rot_deg=-4.0, life_u=life_u, vy_em=0.45)
        dt = (life_u - 0.35) / 0.65
        x = x0 + 0.04 + 0.14 * math.sin(3.2 * dt + ph) + 0.10 * (u4 - 0.4) * dt
        y = (y_top + 0.48) - (0.34 + 0.20 * u6) * dt
        rot = (u11 - 0.5) * 70.0 + (55.0 if u12 > 0.5 else -55.0) * dt
        return Particle(x, y, 0.058 + 0.044 * u7, 0.92 * math.sin(math.pi * dt) ** 0.65, "petal",
                        _lerp(col, (255, 206, 228), 0.50), rot_deg=rot, life_u=life_u)
    if kind == "pollen":
        ang = ph + (2.4 + 0.8 * u9) * age
        rad = 0.10 + 0.12 * u4
        x = x0 + math.cos(ang) * rad
        y = -0.15 - 0.45 * u5 - (0.30 + 0.22 * u6) * age
        sub = "petal" if (k % 3 == 0) else "pollen"
        rot = (u11 - 0.5) * 80.0 + 45.0 * age
        return Particle(x, y, 0.046 + 0.042 * u7, 0.92 * env, sub,
                        _lerp(col, (255, 234, 145), 0.45), rot_deg=rot, life_u=life_u,
                        core_color=(255, 250, 215))
    if kind == "corona":
        ang = ph + (1.2 + 0.5 * u9) * age
        rx = 0.22 + 0.18 * u4
        ry = 0.14 + 0.12 * u5
        x = x0 + math.cos(ang) * rx
        y = -0.45 + math.sin(ang) * ry - 0.08 * age
        return Particle(x, y, 0.052 + 0.044 * u7, 0.92 * env, "corona",
                        _lerp(col, (255, 222, 140), 0.48), rot_deg=math.degrees(ang), life_u=life_u,
                        core_color=(255, 252, 224))
    if kind == "starburst":
        spread_ang = -math.pi * 0.5 + (u4 - 0.5) * 1.35
        speed = 0.55 + 0.45 * u6
        buoy = 1.0 - (1.0 - life_u) ** 1.4
        vx = math.cos(spread_ang) * speed
        vy = math.sin(spread_ang) * speed
        x = x0 + vx * life * buoy
        y = -0.22 - 0.42 * u5 + vy * life * buoy
        return Particle(x, y, 0.048 + 0.040 * u7, 0.94 * env, "starburst",
                        _lerp(col, (255, 168, 72), 0.55), rot_deg=math.degrees(spread_ang), life_u=life_u,
                        vx_em=vx, vy_em=vy, core_color=(255, 248, 205))
    if kind == "harvest":
        x = x0 + (0.22 + 0.16 * u4) * age + 0.06 * math.sin(1.4 * age + ph)
        y = -0.62 - 0.35 * u5 + (0.14 + 0.10 * u6) * age
        rot = (u11 - 0.5) * 60.0 + 25.0 * math.sin(1.6 * age + ph)
        return Particle(x, y, 0.056 + 0.044 * u7, 0.90 * env, "harvest",
                        _lerp(col, (255, 218, 130), 0.45), rot_deg=rot, life_u=life_u)
    if kind == "equinox":
        if k % 2 == 0:
            x = x0 + 0.10 * (u4 - 0.5) * age + 0.07 * math.sin(1.6 * age + ph)
            y = -0.35 - 0.30 * u5 - (0.16 + 0.12 * u6) * age
            return Particle(x, y, 0.052 + 0.040 * u7, 0.80 * env, "equinox",
                            _lerp(col, (176, 224, 212), 0.55), life_u=life_u, core_color=(240, 255, 250))
        x = x0 + 0.14 * math.sin(2.4 * age + ph) + 0.06 * (u4 - 0.5) * age
        y = -0.58 - 0.32 * u5 + (0.20 + 0.14 * u6) * age
        rot = 36.0 * math.cos(2.4 * age + ph) + (u11 - 0.5) * 24.0
        return Particle(x, y, 0.062 + 0.042 * u7, 0.90 * env, "leaf",
                        _lerp(col, (224, 152, 92), 0.55), rot_deg=rot, life_u=life_u)
    if kind == "spectral":
        if k % 3 == 0:
            x = x0 + 0.16 * (u4 - 0.45) * age + 0.10 * math.sin(1.2 * age + ph)
            y = -0.25 - 0.45 * u5 - (0.14 + 0.10 * u6) * age
            r = (0.12 + 0.09 * u7) * (0.85 + 0.35 * life_u)
            return Particle(x, y, r, 0.42 * env, "mist", _lerp(col, (186, 148, 245), 0.55), life_u=life_u)
        x = x0 + 0.16 * math.sin(2.2 * age + ph) + 0.07 * noise1(zp ^ 0x9E37, age * 2.5)
        y = -0.18 - 0.46 * u5 - (0.22 + 0.16 * u6) * age
        return Particle(x, y, 0.046 + 0.038 * u7, 0.86 * env, "spectral",
                        _lerp(col, (238, 152, 104), 0.45), life_u=life_u, core_color=(248, 232, 255))
    if kind == "lantern":
        x = x0 + 0.06 * math.sin(1.1 * age + ph) + 0.04 * (u4 - 0.5) * age
        y = -0.12 - 0.42 * u5 - (0.24 + 0.16 * u6) * age
        return Particle(x, y, 0.056 + 0.044 * u7, 0.92 * env, "lantern",
                        _lerp(col, (255, 178, 64), 0.55), life_u=life_u, core_color=(255, 242, 198))
    # snow_star (December): hushed snow crystals + warm returning solstice starlight
    if k % 3 == 0:
        x = x0 + 0.08 * math.sin(1.2 * age + ph) + 0.04 * (u4 - 0.5) * age
        y = -0.35 - 0.55 * u5 - 0.08 * age
        hope = 0.45 + 0.55 * smoothstep(0.25, 0.75, life_u)
        return Particle(x, y, 0.068 + 0.055 * u7, 0.92 * env * hope, "spark",
                        _lerp(col, (255, 238, 186), 0.60), style="celestial",
                        life_u=life_u, core_color=(255, 255, 245))
    x = x0 + 0.07 * (u4 - 0.3) * age + 0.06 * math.sin(1.4 * age + ph)
    y = -0.78 - 0.40 * u5 + (0.18 + 0.14 * u6) * age
    rot = 360.0 * u11 + (30.0 if u12 > 0.5 else -30.0) * age
    return Particle(x, y, 0.056 + 0.048 * u7, 0.90 * env, "snow",
                    _lerp(col, (226, 246, 255), 0.60), rot_deg=rot, life_u=life_u)


_MONTH_PARTICLES = frozenset({
    "prism", "candle", "gale", "dew_petal", "pollen", "corona",
    "starburst", "harvest", "equinox", "spectral", "lantern", "snow_star",
})


def _custom_particle(p: FxParams, k: int, letters_x_em: Sequence[float], letters_w_em: Sequence[float],
                     t: float, begin_s: float, dur: float, onsets: Sequence[float] | None) -> Particle | None:
    """Emit a themed particle across the active word for seasonal, monthly, and elemental signatures."""
    n = len(letters_x_em)
    zp = _seed_k(p.seed, 300, k)
    u0, u1, u2 = _u01(zp, 0), _u01(zp, 1), _u01(zp, 2)
    li = int(u2 * n) % n
    start_s = onsets[li] if (onsets is not None and len(onsets) == n) else begin_s
    spawn = start_s + u0 * max(0.30, dur * 0.85)
    kind = p.particle_kind
    if kind == "ember":
        # Mix quick bright cinder sparks (~35%) with buoyant flame-lick embers (~65%)
        life = (0.58 + 0.34 * u1) if (k % 3 == 0) else (1.02 + 0.52 * u1)
    elif kind == "shard":
        life = 0.9 + 0.5 * u1
    elif kind == "starburst":
        life = 0.85 + 0.45 * u1
    elif kind == "gale":
        life = 1.0 + 0.45 * u1
    elif kind in ("crimson", "dew_petal"):
        life = 1.65 + 0.55 * u1
    elif kind in ("lantern", "snow_star"):
        life = 1.85 + 0.65 * u1
    else:
        life = 1.5 + 0.7 * u1
    age = t - spawn
    if not 0.0 <= age < life:
        return None
    u3, u4, u5, u6, u7 = _u01(zp, 3), _u01(zp, 4), _u01(zp, 5), _u01(zp, 6), _u01(zp, 7)
    x0 = letters_x_em[li] + u3 * letters_w_em[li]
    ph = 2.0 * math.pi * _u01(zp, 10)
    col = _color(p, li, n)
    life_u = min(1.0, age / life)
    env = math.sin(math.pi * life_u) ** 0.75
    if kind in _MONTH_PARTICLES:
        return _month_particle(kind, k, zp, p, x0, age, life, life_u, env, ph, col, u4, u5, u6, u7,
                               _u01(zp, 9), _u01(zp, 11), _u01(zp, 12))
    if kind == "petal":
        u8, u11, u12 = _u01(zp, 8), _u01(zp, 11), _u01(zp, 12)
        x = x0 + (0.16 + 0.16 * u4) * age + 0.13 * math.sin(2.3 * age + ph)
        y = -0.15 - 0.55 * u5 - (0.34 + 0.28 * u6) * age + 0.05 * math.sin(3.4 * age + ph)
        rot = (u11 - 0.5) * 90.0 + (70.0 if u12 > 0.5 else -70.0) * age
        col = _lerp(col, (255, 210, 226) if u8 < 0.65 else (255, 255, 255), 0.35)
        return Particle(x, y, 0.065 + 0.050 * u7, 0.92 * env, "petal", col, rot_deg=rot, life_u=life_u)
    if kind == "firefly":
        u9, u11 = _u01(zp, 9), _u01(zp, 11)
        x = x0 + 0.20 * math.sin(1.9 * age + ph) + 0.09 * (u4 - 0.5) * age + 0.06 * math.cos(0.9 * age + ph * 0.5)
        y = -0.25 - 0.65 * u5 - (0.16 + 0.14 * u6) * age + 0.15 * math.cos(1.7 * age + ph * 0.7)
        blink = 0.28 + 0.72 * (math.sin(math.pi * ((1.2 + 0.9 * u9) * age + u11)) ** 2)
        return Particle(x, y, 0.045 + 0.045 * u7, 0.98 * env * blink, "firefly", _lerp(col, (255, 232, 145), 0.45), life_u=life_u)
    if kind == "leaf":
        u8, u11 = _u01(zp, 8), _u01(zp, 11)
        vx_osc = 0.22 * 3.1 * math.cos(3.1 * age + ph)
        x = x0 + 0.14 * (u4 - 0.35) * age + 0.22 * math.sin(3.1 * age + ph)
        y = -0.60 - 0.45 * u5 + (0.30 + 0.24 * u6) * age + 0.08 * age * age
        rot = 48.0 * math.cos(3.1 * age + ph) + 12.0 * math.tanh(vx_osc) + (u11 - 0.5) * 28.0
        col = _lerp(col, (242, 168, 72) if u8 < 0.5 else (214, 98, 62), 0.40)
        return Particle(x, y, 0.070 + 0.050 * u7, 0.94 * env, "leaf", col, rot_deg=rot, life_u=life_u)
    if kind == "snow":
        u11, u12 = _u01(zp, 11), _u01(zp, 12)
        x = x0 + 0.09 * (u4 - 0.25) * age + 0.08 * math.sin(1.9 * age + ph)
        y = -0.80 - 0.45 * u5 + (0.24 + 0.18 * u6) * age
        rot = 360.0 * u11 + (42.0 if u12 > 0.5 else -42.0) * age
        return Particle(x, y, 0.060 + 0.055 * u7, 0.92 * env, "snow", _lerp(col, (235, 249, 255), 0.60), rot_deg=rot, life_u=life_u)
    if kind == "ember":
        u9 = _u01(zp, 9)
        # Slow-out convective buoyancy (rises swiftly then decelerates softly as embers cool)
        buoy = 1.0 - (1.0 - life_u) ** 1.55
        rise_dist = (0.90 + 0.65 * u6) * life
        y = -0.08 - 0.48 * u5 - rise_dist * buoy
        vy = -(rise_dist / life) * 1.55 * ((1.0 - life_u) ** 0.55)
        sway_f = 4.2 + 2.2 * u9
        curl = 0.055 * noise1(zp ^ 0x517CC1B7, age * 3.4)
        x = x0 + (u4 - 0.5) * 0.34 * (life_u ** 0.85) * life + 0.12 * math.sin(sway_f * age + ph) + curl
        vx = (u4 - 0.5) * 0.30 + 0.12 * sway_f * math.cos(sway_f * age + ph)
        # Organic flame-align tilt + dual-wave flicker
        rot = max(-38.0, min(38.0, math.degrees(math.atan2(vx, max(0.15, -vy))) * 0.65))
        flicker = 0.60 + 0.40 * (0.5 + 0.5 * math.sin(13.5 * age + ph) * math.cos(5.3 * age + ph * 0.4))
        fade_in = smoothstep(0.0, 0.08, life_u)
        fade_out = (1.0 - life_u) ** 1.25
        # Cooling gradient: luminous white-gold -> vibrant amber -> deep ember crimson
        hot_col = _lerp(col, (255, 224, 128), 0.55)
        mid_col = _lerp(col, (255, 118, 34), 0.70)
        cool_col = (212, 54, 20)
        body_col = _lerp(hot_col, mid_col, life_u * 2.0) if life_u < 0.5 else _lerp(mid_col, cool_col, (life_u - 0.5) * 2.0)
        core_col = _lerp((255, 250, 218), (255, 170, 72), life_u ** 0.8)
        r_base = (0.036 + 0.032 * u7) if (k % 3 == 0) else (0.048 + 0.046 * u7)
        r = r_base * (1.0 - 0.42 * life_u)
        return Particle(x, y, r, 0.96 * fade_in * fade_out * flicker, "ember", body_col,
                        rot_deg=rot, life_u=life_u, vx_em=vx, vy_em=vy, core_color=core_col)
    if kind == "raindrop":
        fall_in = smoothstep(0.0, 0.10, life_u)
        y = -0.55 - 0.40 * u5 + (0.48 + 0.34 * u6) * age + 0.45 * age * age
        vy = (0.48 + 0.34 * u6) + 0.90 * age
        x = x0 + 0.06 * age
        return Particle(x, y, 0.046 + 0.036 * u7, 0.90 * env * fall_in, "raindrop",
                        _lerp(col, (175, 215, 255), 0.45), rot_deg=-5.5, life_u=life_u, vy_em=vy)
    if kind == "tear":
        # Swells at letter bottom (0..0.24s), drips & accelerates (0.24..1.00s), splashes into ripple (1.00..1.45s)
        t_col = _lerp(col, (185, 224, 255), 0.55)
        y_base = 0.08 + 0.04 * u5
        r0 = 0.052 + 0.036 * u7
        if age < 0.24:
            u = age / 0.24
            return Particle(x0, y_base + 0.04 * u, r0 * (0.4 + 0.6 * math.sin(u * math.pi * 0.5)), 0.90 * min(1.0, u * 2.2), "tear", t_col)
        if age < 1.00:
            dt = age - 0.24
            y = y_base + 0.04 + 0.14 * dt + 1.15 * dt * dt
            return Particle(x0, y, r0, 0.92, "tear", t_col)
        u = min(1.0, (age - 1.00) / 0.50)
        y_pool = y_base + 0.04 + 0.14 * 0.76 + 1.15 * 0.76 * 0.76
        return Particle(x0, y_pool, r0 * (1.2 + 4.2 * u), 0.75 * (1.0 - u) ** 1.4, "ripple", _lerp(t_col, (230, 245, 255), 0.4))
    if kind == "crimson":
        # Viscous bleed: swells at letter cut (0..0.34s), drips slowly (0.34..1.30s), stains into dark wine pool (1.30..1.75s)
        c_edge = _lerp(col, (217, 43, 75), 0.72)
        c_wine = _lerp(c_edge, (110, 18, 36), 0.65)
        y_base = -0.04 + 0.16 * u5
        r0 = 0.056 + 0.040 * u7
        if age < 0.34:
            u = age / 0.34
            return Particle(x0, y_base + 0.03 * u, r0 * (0.35 + 0.65 * math.sin(u * math.pi * 0.5)), 0.92 * min(1.0, u * 2.0), "crimson", c_edge)
        if age < 1.30:
            dt = age - 0.34
            u = dt / 0.96
            y = y_base + 0.03 + 0.08 * dt + 0.48 * dt * dt
            return Particle(x0, y, r0, 0.94, "crimson", _lerp(c_edge, c_wine, u * 0.85))
        u = min(1.0, (age - 1.30) / 0.45)
        y_pool = y_base + 0.03 + 0.08 * 0.96 + 0.48 * 0.96 * 0.96
        return Particle(x0, y_pool, r0 * (1.0 + 2.2 * u), 0.78 * (1.0 - u) ** 1.5, "stain", c_wine)
    if kind == "bubble":
        x = x0 + 0.06 * (u4 - 0.5) * age + 0.10 * math.sin(3.0 * age + ph)
        y = -0.05 - 0.40 * u5 - (0.36 + 0.25 * u6) * age
        return Particle(x, y, 0.055 + 0.055 * u7, 0.86 * env, "bubble", _lerp(col, (190, 244, 255), 0.5), life_u=life_u)
    if kind == "mist":
        x = x0 + 0.20 * (u4 - 0.4) * age + 0.12 * math.sin(1.1 * age + ph)
        y = -0.22 - 0.55 * u5 - (0.14 + 0.12 * u6) * (life * (1.0 - (1.0 - life_u) ** 1.3))
        r = (0.14 + 0.11 * u7) * (0.82 + 0.42 * life_u)
        return Particle(x, y, r, 0.44 * env, "mist", _lerp(col, (240, 248, 255), 0.55), life_u=life_u)
    if kind == "shard":
        u11, u12 = _u01(zp, 11), _u01(zp, 12)
        vx = (u4 - 0.5) * 0.72
        x = x0 + vx * age
        y = -0.25 - 0.35 * u5 + (0.20 * u6 - 0.15) * age + 0.55 * age * age
        rot = 360.0 * u11 + (220.0 if u12 > 0.5 else -220.0) * age * (1.0 - 0.25 * life_u)
        return Particle(x, y, 0.060 + 0.048 * u7, 0.94 * (1.0 - life_u), "shard", _lerp(col, (235, 245, 255), 0.55), rot_deg=rot, life_u=life_u)
    if kind == "wisp":
        u9 = _u01(zp, 9)
        w_rate = 1.6 + 0.8 * u9
        ang = ph + w_rate * age
        rad = 0.15 + 0.19 * u4
        rise = 0.18 if p.sig_axis == "scale" else 0.14
        x = x0 + math.cos(ang) * rad
        y = -0.42 - 0.35 * u5 - rise * age + math.sin(ang) * rad * 0.55
        vx = -math.sin(ang) * rad * w_rate
        vy = -rise + math.cos(ang) * rad * 0.55 * w_rate
        return Particle(x, y, 0.048 + 0.042 * u7, 0.86 * env, "wisp",
                        _lerp(col, (232, 222, 255), 0.45), life_u=life_u, vx_em=vx, vy_em=vy,
                        core_color=(255, 252, 245))
    if kind == "spark":
        u9, u11 = _u01(zp, 9), _u01(zp, 11)
        y = -0.20 - 0.65 * u5 - (0.22 + 0.18 * u6) * age
        x = x0 + 0.12 * math.sin(1.8 * age + ph) + 0.06 * (u4 - 0.5) * age
        tw = 0.25 + 0.75 * (math.sin(math.pi * ((1.0 + 0.7 * u9) * age + u11)) ** 2)
        return Particle(x, y, 0.075 + 0.065 * u7, 0.90 * env * tw, "spark",
                        _lerp(col, (255, 255, 255), 0.45), style=p.sparkle_style or "celestial",
                        life_u=life_u, core_color=(255, 255, 255))
    # seed: differentiate upward buoyant growth spiral vs horizontal wind-swept air seeds
    if p.sig_axis == "scale" or p.drift_em < -0.05:
        ang = ph + 2.2 * age
        x = x0 + 0.14 * math.sin(ang) + 0.12 * (u4 - 0.5) * age
        y = -0.18 - 0.45 * u5 - (0.34 + 0.22 * u6) * age
        rot = 16.0 * math.cos(ang)
    else:
        x = x0 + (0.50 + 0.34 * u4) * age
        y = -0.25 - 0.50 * u5 - 0.18 * age + 0.11 * math.sin(2.5 * age + ph)
        rot = 24.0 + 18.0 * math.cos(2.5 * age + ph)
    return Particle(x, y, 0.062 + 0.045 * u7, 0.88 * env, "seed", _lerp(col, (240, 250, 255), 0.65), rot_deg=rot, life_u=life_u)


def _emit_sparkles(p: FxParams, letters_x_em: Sequence[float], letters_w_em: Sequence[float],
                   t: float, begin_s: float, end_s: float,
                   fill_front_u: float | None, out: list[Particle]) -> None:
    if p.sparkle <= 0 or not (begin_s - 0.1 <= t <= end_s + 3.0):
        return
    n = len(letters_x_em)
    win = smoothstep(begin_s - 0.1, begin_s + 0.2, t) * (1.0 - smoothstep(end_s + 2.5, end_s + 3.0, t))
    width = max(x + w for x, w in zip(letters_x_em, letters_w_em))
    front_x = (fill_front_u * width) if fill_front_u is not None else None
    style = p.sparkle_style or "star"
    dt_t = max(0.0, t - begin_s)
    for k in range(p.sparkle):
        zp = _seed_k(p.seed, 200, k)
        u0, u1, u2, u3, u4 = _u01(zp, 0), _u01(zp, 1), _u01(zp, 2), _u01(zp, 3), _u01(zp, 4)
        u = u2 * 1.2 - 0.1
        if front_x is not None:
            x = min(width * 1.05, max(-0.05 * width, front_x + (u2 - 0.5) * 0.38 * width))
            ci = min(n - 1, max(0, int((x / max(width, 1e-4)) * n)))
        else:
            x = u * width
            ci = min(n - 1, max(0, int(u * n)))
        base_col = _color(p, ci, n)
        if style == "celestial":
            # Hierarchical starlight: 1/3 hero diffraction glints + 2/3 pinpoint stars with curved orbital drift
            is_hero = (k % 3 == 0)
            freq = 0.65 + 0.75 * u0
            tw = 0.18 + 0.82 * ((0.5 + 0.5 * math.sin(2.0 * math.pi * (freq * t + u1))) ** 2.2)
            ang = 2.0 * math.pi * u1 + 0.55 * dt_t
            dx_arc = 0.065 * math.cos(ang)
            dy_arc = -0.045 * math.sin(ang)
            r = (0.14 + 0.07 * u4) if is_hero else (0.058 + 0.052 * u4)
            col = _lerp(base_col, (232, 240, 255), 0.45)
            out.append(Particle(x + dx_arc, -1.08 + 1.30 * u3 + dy_arc, r, tw * win, "spark", col,
                                style="celestial", core_color=(255, 255, 255)))
        elif style == "ember":
            # Warm cinder sparks rising above the active word
            cyc = (dt_t * (0.85 + 0.45 * u0) + u1) % 1.0
            tw = math.sin(math.pi * cyc) ** 0.7 * (0.65 + 0.35 * math.sin(18.0 * t + 6.28 * u1))
            x_e = x + 0.08 * math.sin(5.0 * cyc + 6.28 * u0)
            y_e = -0.15 - 0.78 * u3 - 0.45 * cyc
            col = _lerp(base_col, (255, 168, 56), 0.62)
            out.append(Particle(x_e, y_e, 0.055 + 0.055 * u4, max(0.0, tw) * win, "spark", col,
                                style="ember", life_u=cyc, vy_em=-0.55, core_color=(255, 248, 212)))
        elif style == "buoyant":
            # Joyful upward-arcing glints with quick upbeat pulse
            freq = 1.05 + 0.85 * u0
            tw = max(0.0, math.sin(2.0 * math.pi * (freq * t + u1))) ** 1.6
            arc = 0.10 * math.sin(math.pi * ((dt_t * 0.9 + u1) % 1.0))
            col = _lerp(base_col, (255, 246, 224), 0.45)
            out.append(Particle(x, -0.92 + 1.15 * u3 - arc, 0.095 + 0.095 * u4, tw * win, "spark", col,
                                style="buoyant", vy_em=-0.35, core_color=(255, 252, 236)))
        elif style == "bokeh":
            # Soft romantic / warm breathing bokeh motes
            freq = 0.45 + 0.55 * u0
            tw = 0.22 + 0.78 * (0.5 + 0.5 * math.sin(2.0 * math.pi * (freq * t + u1))) ** 1.5
            drift_y = -0.08 * math.sin(1.4 * dt_t + 6.28 * u1)
            col = _lerp(base_col, (255, 236, 225), 0.40)
            out.append(Particle(x, -0.95 + 1.15 * u3 + drift_y, 0.085 + 0.095 * u4, 0.75 * tw * win, "spark", col,
                                style="bokeh", core_color=(255, 248, 238)))
        elif style == "flare":
            # Specular filmic lens glints near upper silhouette
            freq = 0.60 + 0.70 * u0
            tw = max(0.0, math.sin(2.0 * math.pi * (freq * t + u1))) ** 2.2
            col = _lerp(base_col, (255, 248, 232), 0.55)
            out.append(Particle(x, -0.88 + 0.75 * u3, 0.10 + 0.10 * u4, tw * win, "spark", col,
                                style="flare", core_color=(255, 252, 242)))
        else:
            tw = max(0.0, math.sin(2.0 * math.pi * ((0.8 + u0) * t + u1))) ** 2
            out.append(Particle(x, -1.0 + 1.25 * u3, 0.11 + 0.12 * u4, tw * win, "spark",
                                _lerp(base_col, (255, 255, 255), 0.5), style="star"))


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
        eroded_raw = _eroded(p, n)
        ne = max(eroded_raw, 1)  # sin erosión el polvo sale de la última letra
        for k in range(max(p.particles, 0)):
            zp = _seed_k(p.seed, 100, k)
            u0, u1, u2 = _u01(zp, 0), _u01(zp, 1), _u01(zp, 2)
            li = n - ne + int(u2 * ne) % ne
            if onsets is not None and len(onsets) == n:
                spawn = onsets[li] + u0 * min(0.25, max(0.05, end_s - onsets[li]))
            else:
                spawn = (p.vanish_s + u0 * VANISH_S if p.vanish_s > 0.0 else end_s - ERODE_TAIL * dur + u0 * (ERODE_TAIL * dur + DRIFT_TAIL_S))
            life = 1.0 + 0.4 * u1
            age = t - spawn
            if not 0.0 <= age < life:
                continue
            u3, u4, u5, u6, u7, u8, u9, u10 = (_u01(zp, s) for s in range(3, 11))
            life_u = age / life
            damp = (1.0 - math.exp(-2.0 * age)) * 0.5
            x = (letters_x_em[li] + u3 * letters_w_em[li] + (u4 - 0.4) * 0.40 * damp
                 + 0.03 * math.sin(2.0 * math.pi * ((0.5 + 0.4 * u9) * age + u10))
                 + 0.02 * noise1(zp ^ 0x3C6EF372, age * 2.2))
            if eroded_raw > 0:
                x = max(letters_x_em[n - ne], x)
            y0 = p.drift_em - 0.1 - 0.5 * u5  # las letras erosionadas ya bajaron/subieron con el drift
            if p.drift_em >= 0.0:
                y = y0 - 0.1 * u6 * age + 0.5 * GRAVITY * age * age
            else:
                y = y0 - (0.2 + 0.2 * u6) * age
            col = _color(p, li, n)
            if u8 < 0.25:  # una de cada cuatro, más clara
                col = _lerp(col, (255, 255, 255), 0.4)
            out.append(Particle(x, y, 0.035 + 0.055 * u7, 0.95 * (1.0 - life_u) ** 1.5, "dust", col, life_u=life_u))

    _emit_sparkles(p, letters_x_em, letters_w_em, t, begin_s, end_s, fill_front_u, out)
    return out
