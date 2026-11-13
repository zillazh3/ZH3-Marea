"""Song-level emotional momentum ('Undertow') for Semantic Word FX.

Maintains a continuous 6-axis emotional vector smoothed with a critically damped
spring at 240 Hz (tau ~ 1.5-3.0 s scaled by mood profile) so every word and
instrumental gap shares an evolving emotional ground tone instead of isolated islands.
"""
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from marea.wordfx.fxtypes import AXES, RGB
from marea.wordfx.profile import vector_at as profile_vector_at
from marea.wordfx.springs import SIM_DT, step_spring

ZERO_VEC: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
MAX_AMBIENT_AMP = 0.15
MAX_INTERLUDE_TINT = 0.20

_WARM_POS: RGB = (255, 198, 128)  # warm luminous amber
_COOL_POS: RGB = (164, 224, 255)  # airy sky cyan
_WARM_NEG: RGB = (232, 134, 152)  # tender bruised rose
_COOL_NEG: RGB = (122, 168, 238)  # deep nostalgic indigo-blue


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _lerp_rgb(a: RGB, b: RGB, u: float) -> RGB:
    k = _clamp(u, 0.0, 1.0)
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))  # type: ignore[return-value]


def ambient_vector_color(vec: Sequence[float]) -> RGB:
    """Map a 6-axis Undertow vector cleanly to a cohesive ambient tint RGB."""
    v = vec[0] if len(vec) > 0 else 0.0
    a = vec[1] if len(vec) > 1 else 0.0
    t = vec[3] if len(vec) > 3 else 0.0
    wu = _clamp(0.5 + 0.5 * t, 0.0, 1.0)
    vu = _clamp(0.5 + 0.5 * v, 0.0, 1.0)
    neg = _lerp_rgb(_COOL_NEG, _WARM_NEG, wu)
    pos = _lerp_rgb(_COOL_POS, _WARM_POS, wu)
    base = _lerp_rgb(neg, pos, vu)
    # High arousal nudges slightly brighter so energy reads naturally in tint
    return _lerp_rgb(base, (255, 244, 228), _clamp(0.15 * max(0.0, a), 0.0, 0.15))


def mood_tau_s(profile=None) -> float:
    """Return momentum time constant in [1.5, 3.0] s scaled by the song's mood profile."""
    if profile is None:
        return 2.15
    moods = set(getattr(profile, "moods", ()) or ())
    if moods & {"euphoric", "energetic", "angry", "defiant"}:
        return 1.60
    if moods & {"sad", "nostalgic", "melancholic", "tender", "dreamy"}:
        return 2.75
    return 2.15


@dataclass(frozen=True)
class MomentumCurve:
    """Pre-simulated deterministic song-level Undertow trajectory."""
    samples: np.ndarray  # shape (N, 6), float32
    rate_hz: float = 60.0
    tau_s: float = 2.15

    def vector_at(self, t_s: float) -> tuple[float, ...]:
        """Linearly interpolate the 6-axis momentum vector at time `t_s`."""
        n = len(self.samples)
        if n == 0:
            return ZERO_VEC
        pos = _clamp(t_s * self.rate_hz, 0.0, float(n - 1))
        i0 = int(math.floor(pos))
        i1 = min(n - 1, i0 + 1)
        u = pos - i0
        v0 = self.samples[i0]
        v1 = self.samples[i1]
        return tuple(float(v0[k] + (v1[k] - v0[k]) * u) for k in range(6))

    def color_at(self, t_s: float) -> RGB:
        """Color derived from the live Undertow state vector at `t_s`."""
        return ambient_vector_color(self.vector_at(t_s))

    def ambient_tint_weight(self, t_s: float, intensity: float = 1.0) -> float:
        """Modulation amplitude for non-semantic/ambient words, strictly capped <= 15%."""
        vec = self.vector_at(t_s)
        mag = min(1.0, math.sqrt(sum(x * x for x in vec) / 3.0))
        return _clamp(MAX_AMBIENT_AMP * (0.45 + 0.55 * mag) * min(1.0, max(0.0, intensity)), 0.0, MAX_AMBIENT_AMP)

    def interlude_color(self, t_s: float, intensity: float = 1.0) -> RGB:
        """Dot color for instrumental interludes: white tinted up to <= 20% by momentum."""
        vec = self.vector_at(t_s)
        mag = min(1.0, math.sqrt(sum(x * x for x in vec) / 3.0))
        w = _clamp(MAX_INTERLUDE_TINT * (0.4 + 0.6 * mag) * min(1.0, max(0.0, intensity)), 0.0, MAX_INTERLUDE_TINT)
        return _lerp_rgb((255, 255, 255), ambient_vector_color(vec), w)


def build_momentum(
    events: Sequence[tuple[float, float, tuple[float, ...], float]],
    duration_s: float,
    profile=None,
    rate_hz: float = 60.0,
) -> MomentumCurve:
    """Simulate 240 Hz critically damped spring momentum over semantic word events.

    `events`: sequence of `(begin_s, end_s, vector_6d, salience_weight)` sorted by `begin_s`.
    """
    tau = mood_tau_s(profile)
    # Critically damped frequency corresponding to settling time tau (~1.5..3.0 s)
    freq_hz = (3.2 / tau) / (2.0 * math.pi)
    total_s = max(1.0, duration_s + 1.0)
    n_out = max(2, int(math.ceil(total_s * rate_hz)) + 1)
    out = np.zeros((n_out, len(AXES)), dtype=np.float32)

    sorted_ev = sorted(events, key=lambda e: e[0])
    # Initial state starts on section baseline at t=0
    p = list(profile_vector_at(profile, 0.0)) if (profile is not None and profile_vector_at(profile, 0.0)) else [0.0] * 6
    v = [0.0] * 6

    steps_per_frame = max(1, round(240.0 / rate_hz))
    ev_idx = 0
    active: list[tuple[float, float, tuple[float, ...], float]] = []

    for fi in range(n_out):
        out[fi] = p
        if fi == n_out - 1:
            break
        t_frame = fi / rate_hz
        for sub in range(steps_per_frame):
            t = t_frame + sub * SIM_DT
            while ev_idx < len(sorted_ev) and sorted_ev[ev_idx][0] <= t + 0.25:
                active.append(sorted_ev[ev_idx])
                ev_idx += 1
            active = [e for e in active if e[1] + 0.45 >= t]

            base = (
                profile_vector_at(profile, t)
                if profile is not None
                else None
            ) or ZERO_VEC

            if active:
                w_tot = 0.0
                acc = [0.0] * 6
                for b0, b1, vec, sal in active:
                    if t < b0 - 0.15:
                        continue
                    w = sal * (0.4 if t > b1 else 1.0)
                    w_tot += w
                    for k in range(6):
                        acc[k] += w * vec[k]
                if w_tot > 1e-6:
                    push = min(1.0, w_tot)
                    target = [
                        base[k] * (1.0 - push) + (acc[k] / w_tot) * push
                        for k in range(6)
                    ]
                else:
                    target = list(base)
            else:
                target = list(base)

            for k in range(6):
                p[k], v[k] = step_spring(p[k], v[k], target[k], freq_hz, 1.0, SIM_DT)

    return MomentumCurve(samples=out, rate_hz=rate_hz, tau_s=tau)
