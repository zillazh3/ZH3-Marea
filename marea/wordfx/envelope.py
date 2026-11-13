"""Continuous 6-phase word/letter envelope and duration modes for Semantic Word FX.

Phases:
1. anticipation (~150-250 ms before word start, hero words only): 10-15% amplitude
2. attack (per letter, keyed to fill-front crossing): spring rise to sustain target
3. sustain (word duration d): Accent (<250ms), Normal (250-700ms), Held (>700ms, log(d) capped)
4. release (400-900 ms after word end): category-dependent decay into residue
5. residue (while line is active): 10-25% settled level
6. fade (line becomes inactive): fades with base line dim/blur, leaving ~5% past-line tint
"""
import math
from dataclasses import dataclass

from marea.wordfx.springs import eval_spring_240hz

ACCENT_MAX_S = 0.25
HELD_MIN_S = 0.70
ACCENT_AMP = 0.40
HELD_AMP_CAP = 1.85
ANTICIPATION_S = 0.20
ANTICIPATION_AMP = 0.13
MIN_RELEASE_S = 0.40
MAX_RELEASE_S = 0.90
MIN_RESIDUE = 0.10
MAX_RESIDUE = 0.25
PAST_LINE_TINT = 0.05


@dataclass(frozen=True)
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


def _smooth01(x: float) -> float:
    u = min(1.0, max(0.0, x))
    return u * u * (3.0 - 2.0 * u)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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
        u = _smooth01((t - anti_start) / max(1e-4, b0 - anti_start))
        return ANTICIPATION_AMP * u
    if t <= b1:
        return peak
    rel_end = b1 + release_s
    if t <= rel_end:
        u = _smooth01((t - b1) / release_s)
        return peak + (residue - peak) * u
    if t <= line_end_s:
        return residue
    u = _smooth01((t - line_end_s) / max(0.05, fade_s))
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
    on_t = b0 if letter_on_t is None else _clamp(letter_on_t, b0, b1_eff)
    rel_s = _clamp(release_s, MIN_RELEASE_S, MAX_RELEASE_S)
    res = _clamp(residue_level, MIN_RESIDUE, MAX_RESIDUE)
    anti_s = _clamp(anticipation_s, 0.15, 0.25) if is_hero else 0.0
    l_end = (b1_eff + rel_s + 1.5) if line_end_s is None else max(b1_eff, line_end_s)

    t_origin = b0 - anti_s - 0.05
    if t <= t_origin:
        return EnvelopeSample("idle", 0.0, 0.0, mode, peak if mode == "held" else 1.0)

    def target_fn(tau: float) -> float:
        return _raw_target(tau, b0, b1_eff, on_t, peak, is_hero, anti_s, rel_s, res, l_end, fade_s)

    if reduce:
        amp = max(0.0, target_fn(t))
    else:
        amp, _ = eval_spring_240hz(target_fn, t, t_origin, initial=0.0, freq_hz=freq_hz, zeta=zeta)
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
        tint = max(PAST_LINE_TINT * act + PAST_LINE_TINT * (1.0 - act), amp)
    else:
        tint = amp

    return EnvelopeSample(phase, amp, tint, mode, peak if mode == "held" else 1.0)
