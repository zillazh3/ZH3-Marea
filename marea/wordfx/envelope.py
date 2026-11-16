"""Continuous 6-phase word/letter envelope and duration modes for Semantic Word FX.

Phases:
1. anticipation (`TUNING.anticipation_s` before word start, default 50 ms, hero words only): ~13% amplitude
2. attack (per letter, keyed to fill-front crossing): spring rise to sustain target
3. sustain (word duration d): Accent (<250ms), Normal (250-700ms), Held (>700ms, log(d) capped)
4. release (400-900 ms after word end): category-dependent decay into residue
5. residue (while line is active): 10-25% settled level
6. fade (line becomes inactive): fades with base line dim/blur, leaving ~5% past-line tint
"""
import math
from collections import OrderedDict
from dataclasses import dataclass

from marea.engine.tuning import TUNING
from marea.wordfx._math import clamp as _clamp, smoothstep
from marea.wordfx.springs import CachedSpringCurve

_ENVELOPE_CURVES: OrderedDict[tuple, CachedSpringCurve] = OrderedDict()
_MAX_ENVELOPE_CURVES = 4096

ACCENT_MAX_S = 0.25
HELD_MIN_S = 0.70
ACCENT_AMP = 0.40
HELD_AMP_CAP = 1.85
ANTICIPATION_S = TUNING.anticipation_s  # same constant as the line lookahead: word and line react together
ANTICIPATION_AMP = 0.13
MIN_RELEASE_S = 0.40
MAX_RELEASE_S = 0.90
MIN_RESIDUE = 0.10
MAX_RESIDUE = 0.25
PAST_LINE_TINT = 0.05


@dataclass(frozen=True, slots=True)
class EnvelopeSample:
    """Evaluated envelope state at time `t`."""
    phase: str  # idle | anticipation | attack | sustain | release | residue | fade
    motion_amp: float  # continuous spring-filtered amplitude for motion/glow/scale
    tint_amp: float  # continuous amplitude for color tint (includes ~5% past-line trail)
    mode: str  # accent | normal | held
    held_factor: float  # >= 1.0 in held mode, grows with log(d), capped


def duration_mode(dur_s: float) -> tuple[str, float]:
    """Return `(mode, sustain_peak)` for word duration `dur_s`."""
    if dur_s < ACCENT_MAX_S:
        return "accent", ACCENT_AMP
    if dur_s <= HELD_MIN_S:
        return "normal", 1.0
    held_scale = min(HELD_AMP_CAP, 1.0 + 0.45 * math.log1p((dur_s - HELD_MIN_S) / 0.50))
    return "held", held_scale


def _raw_target(
    t: float,
    b0: float,
    b1: float,
    on_t: float,
    peak: float,
    is_hero: bool,
    anticipation_s: float,
    release_s: float,
    residue: float,
    line_end_s: float,
    fade_s: float,
) -> float:
    """Piecewise continuous target curve before spring filtering."""
    anti_start = b0 - anticipation_s
    if t < anti_start:
        return 0.0
    if t < on_t:
        if not is_hero or anticipation_s <= 0.0:
            return 0.0
        u = smoothstep(anti_start, b0, t)
        return ANTICIPATION_AMP * u
    if t <= b1:
        return peak
    rel_end = b1 + release_s
    if t <= rel_end:
        u = smoothstep(b1, rel_end, t)
        return peak + (residue - peak) * u
    if t <= line_end_s:
        return residue
    u = smoothstep(line_end_s, line_end_s + max(0.05, fade_s), t)
    return residue * (1.0 - u)


def sample_envelope(
    t: float,
    b0: float,
    b1: float,
    *,
    letter_on_t: float | None = None,
    is_hero: bool = False,
    release_s: float = 0.65,
    residue_level: float = 0.16,
    line_end_s: float | None = None,
    line_act: float | None = None,
    anticipation_s: float = ANTICIPATION_S,
    fade_s: float = 0.45,
    zeta: float = 1.0,
    freq_hz: float = 3.2,
    reduce: bool = False,
) -> EnvelopeSample:
    """Evaluate the 6-phase envelope deterministically via a 240 Hz prewarmed spring.

    `letter_on_t`: timestamp when the fill front crosses this letter (defaults to `b0`).
    `line_end_s`: timestamp when the line ceases to be active (defaults to `b1 + release_s + 1.5`).
    `line_act`: optional live carrier `L.act` (0..1); when provided after `b1 + release_s`, drives fade smoothly.
    """
    dur = max(0.05, b1 - b0)
    b1_eff = b0 + dur
    mode, peak = duration_mode(dur)
    on_t = b0 if letter_on_t is None else (b0 if letter_on_t < b0 else (b1_eff if letter_on_t > b1_eff else letter_on_t))
    rel_s = MIN_RELEASE_S if release_s < MIN_RELEASE_S else (MAX_RELEASE_S if release_s > MAX_RELEASE_S else release_s)
    res = MIN_RESIDUE if residue_level < MIN_RESIDUE else (MAX_RESIDUE if residue_level > MAX_RESIDUE else residue_level)
    anti_s = _clamp(anticipation_s, 0.0, 0.25) if is_hero else 0.0
    l_end = (b1_eff + rel_s + 1.5) if line_end_s is None else (b1_eff if line_end_s < b1_eff else line_end_s)

    t_origin = b0 - anti_s - 0.05
    if t <= t_origin:
        return EnvelopeSample("idle", 0.0, 0.0, mode, peak if mode == "held" else 1.0)

    if reduce:
        amp = max(0.0, _raw_target(t, b0, b1_eff, on_t, peak, is_hero, anti_s, rel_s, res, l_end, fade_s))
    else:
        rel_on = round(on_t - b0, 3)
        rel_l_end = round(l_end - b0, 2)
        q_dur = round(dur, 3)
        q_peak = round(peak, 3)
        # Shift to relative time (t - b0) and quantize metric slots so spring curves are shared across matching durations
        key = (q_dur, rel_on, q_peak, is_hero, anti_s, rel_s, res, rel_l_end, fade_s, zeta, freq_hz)
        curve = _ENVELOPE_CURVES.get(key)
        if curve is None:
            def target_fn(
                tau: float,
                _b1=q_dur, _on=rel_on, _pk=q_peak, _hero=is_hero,
                _anti=anti_s, _rel=rel_s, _res=res, _le=rel_l_end, _fs=fade_s,
            ) -> float:
                return _raw_target(tau, 0.0, _b1, _on, _pk, _hero, _anti, _rel, _res, _le, _fs)

            curve = CachedSpringCurve(
                target_fn,
                -anti_s - 0.05,
                initial=0.0,
                freq_hz=freq_hz,
                zeta=zeta,
                steady_after=rel_l_end + max(0.05, fade_s),
                steady_value=0.0,
            )
            _ENVELOPE_CURVES[key] = curve
            if len(_ENVELOPE_CURVES) > _MAX_ENVELOPE_CURVES:
                _ENVELOPE_CURVES.popitem(last=False)
        elif len(_ENVELOPE_CURVES) >= _MAX_ENVELOPE_CURVES // 2:
            _ENVELOPE_CURVES.move_to_end(key)
        amp, _ = curve.eval(t - b0)
        amp = max(0.0, amp)

    # Phase label for debugging and inspection
    if t < b0:
        phase = "anticipation" if (is_hero and t >= b0 - anti_s) else "idle"
    elif t < on_t:
        phase = "anticipation" if is_hero else "idle"
    elif t <= min(b1_eff, on_t + 0.18):
        phase = "attack"
    elif t <= b1_eff:
        phase = "sustain"
    elif t <= b1_eff + rel_s:
        phase = "release"
    elif (line_act is not None and line_act >= 0.5) or (line_act is None and t <= l_end):
        phase = "residue"
    else:
        phase = "fade"

    # Past lines retain ~5% residual tint so the emotional trail stays visible while scrolling up
    if phase == "fade":
        tint = max(PAST_LINE_TINT * min(1.0, peak), amp)
    elif line_act is not None and t > b1_eff + rel_s:
        act = _clamp(line_act, 0.0, 1.0)
        amp *= act
        tint = max(PAST_LINE_TINT, amp)
    else:
        tint = amp

    return EnvelopeSample(phase, amp, tint, mode, peak if mode == "held" else 1.0)
