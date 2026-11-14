"""Song-level emotional momentum ('Undertow') for Semantic Word FX.

Maintains a continuous 6-axis emotional vector smoothed with a critically damped
spring at 240 Hz (tau ~ 1.5-3.0 s scaled by mood profile) so every word and
instrumental gap shares an evolving emotional ground tone instead of isolated islands.
"""
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from marea.wordfx._math import MAX_AMBIENT_AMP, ambient_vector_color, clamp as _clamp, lerp_rgb as _lerp_rgb
from marea.wordfx.fxtypes import AXES, RGB
from marea.wordfx.profile import _global_vector, vector_at as profile_vector_at
from marea.wordfx.springs import SIM_DT, _spring_step_coeffs

ZERO_VEC: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
MAX_INTERLUDE_TINT = 0.20

__all__ = [
    "MAX_AMBIENT_AMP",
    "MAX_INTERLUDE_TINT",
    "MomentumCurve",
    "ZERO_VEC",
    "ambient_vector_color",
    "build_momentum",
    "mood_tau_s",
]


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
    a11, a12, a21, a22 = _spring_step_coeffs(freq_hz, 1.0, SIM_DT)
    total_s = max(1.0, duration_s + 1.0)
    n_out = max(2, int(math.ceil(total_s * rate_hz)) + 1)
    out = np.zeros((n_out, len(AXES)), dtype=np.float32)

    sorted_ev = sorted(events, key=lambda e: e[0])
    n_ev = len(sorted_ev)
    g_vec = _global_vector(profile) if profile is not None else None
    has_arc = bool(profile is not None and profile.arc)
    init_vec = (profile_vector_at(profile, 0.0, global_vec=g_vec) if has_arc else g_vec) or ZERO_VEC
    p = list(init_vec)
    v = [0.0] * 6
    static_base = ZERO_VEC if has_arc else (g_vec or ZERO_VEC)

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
            while ev_idx < n_ev and sorted_ev[ev_idx][0] <= t + 0.25:
                active.append(sorted_ev[ev_idx])
                ev_idx += 1
            if active:
                active = [e for e in active if e[1] + 0.45 >= t]

            base = (profile_vector_at(profile, t, global_vec=g_vec) or ZERO_VEC) if has_arc else static_base

            if active:
                w_tot = 0.0
                a0 = a1 = a2 = a3 = a4 = a5 = 0.0
                for b0, b1, vec, sal in active:
                    if t < b0 - 0.15:
                        continue
                    w = sal * (0.4 if t > b1 else 1.0)
                    w_tot += w
                    a0 += w * vec[0]
                    a1 += w * vec[1]
                    a2 += w * vec[2]
                    a3 += w * vec[3]
                    a4 += w * vec[4]
                    a5 += w * vec[5]
                if w_tot > 1e-6:
                    push = min(1.0, w_tot)
                    rem = 1.0 - push
                    inv = push / w_tot
                    target = (
                        base[0] * rem + a0 * inv,
                        base[1] * rem + a1 * inv,
                        base[2] * rem + a2 * inv,
                        base[3] * rem + a3 * inv,
                        base[4] * rem + a4 * inv,
                        base[5] * rem + a5 * inv,
                    )
                else:
                    target = base
            else:
                target = base

            for k in range(6):
                gk = target[k]
                ok = p[k] - gk
                vk = v[k]
                if abs(ok) < 1e-12 and abs(vk) < 1e-12:
                    p[k], v[k] = gk, 0.0
                else:
                    p[k] = a11 * ok + a12 * vk + gk
                    v[k] = a21 * ok + a22 * vk

    return MomentumCurve(samples=out, rate_hz=rate_hz, tau_s=tau)
