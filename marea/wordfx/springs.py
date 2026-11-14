"""Deterministic spring physics for Semantic Word FX ("Undertow").

Provides both closed-form analytical spring transitions (critically damped, underdamped, and overdamped)
and a fixed-timestep 240 Hz evaluator anchored to a fixed origin time so seeking directly to `t`
produces bit-identical results to linear playback at any frame rate.
"""
import math
from array import array
from collections.abc import Callable
from dataclasses import dataclass

from marea.engine.springs import step_spring

SIM_HZ = 240.0
SIM_DT = 1.0 / SIM_HZ
DEFAULT_FREQ_HZ = 2.4
DEFAULT_ZETA = 1.0  # critically damped by default


def _spring_step_coeffs(freq_hz: float, zeta: float, dt: float) -> tuple[float, float, float, float]:
    """Precompute the 2x2 analytical state-transition matrix `(a11, a12, a21, a22)` for fixed `(freq_hz, zeta, dt)`."""
    if dt <= 0.0 or freq_hz <= 0.0:
        return 1.0, 0.0, 0.0, 1.0
    w = freq_hz * 2.0 * math.pi
    if abs(zeta - 1.0) < 1e-6:
        q = math.exp(-w * dt)
        t_q = dt * q
        return q + t_q * w, t_q, -(t_q * w * w), q - t_q * w
    if zeta < 1.0:
        z = max(0.01, zeta)
        q = math.exp(-z * w * dt)
        c = math.sqrt(1.0 - z * z)
        cs = math.cos(dt * w * c)
        sn = math.sin(dt * w * c)
        k1 = sn / c
        k2 = sn / (w * c)
        return (cs + k1 * z) * q, k2 * q, -(k1 * w) * q, (cs - k1 * z) * q
    c = math.sqrt(zeta * zeta - 1.0)
    r1 = -w * (zeta + c)
    r2 = -w * (zeta - c)
    e1 = math.exp(r1 * dt)
    e2 = math.exp(r2 * dt)
    inv = 1.0 / (2.0 * w * c)
    de = e2 - e1
    der = e2 * r2 - e1 * r1
    return e1 - r1 * inv * de, inv * de, e1 * r1 - r1 * inv * der, inv * der


@dataclass
class SpringChannel:
    """Mutable velocity-preserving spring channel for fixed-timestep simulation."""
    p: float = 0.0
    v: float = 0.0
    g: float = 0.0
    freq_hz: float = DEFAULT_FREQ_HZ
    zeta: float = DEFAULT_ZETA

    def snap(self, value: float) -> None:
        self.p = value
        self.v = 0.0
        self.g = value

    def retarget(self, target: float, freq_hz: float | None = None, zeta: float | None = None) -> None:
        """Change target (and optionally stiffness/damping) while preserving current position and velocity."""
        self.g = target
        if freq_hz is not None:
            self.freq_hz = freq_hz
        if zeta is not None:
            self.zeta = zeta

    def step(self, dt: float) -> float:
        self.p, self.v = step_spring(self.p, self.v, self.g, self.freq_hz, self.zeta, dt)
        return self.p


def eval_spring_240hz(
    target_fn: Callable[[float], float],
    t: float,
    t_origin: float,
    initial: float = 0.0,
    freq_hz: float = DEFAULT_FREQ_HZ,
    zeta: float = DEFAULT_ZETA,
    max_ticks: int = 2400,
) -> tuple[float, float]:
    """Evaluate a spring driven by `target_fn(time)` from fixed `t_origin` up to `t` at 240 Hz.

    Because `t_origin` is fixed for a given word/event and ticks advance at `SIM_DT = 1/240`,
    any direct seek to `t` produces the exact same `(p, v)` as linear playback at 30/60/240 fps.
    A bounded `max_ticks` window prevents unbounded CPU work on very long pauses; after `max_ticks`,
    the remaining fractional remainder is evaluated analytically toward the steady target.
    """
    if t <= t_origin:
        return initial, 0.0
    elapsed = t - t_origin
    n_ticks = int(math.floor(elapsed * SIM_HZ))
    p, v = initial, 0.0
    if n_ticks > max_ticks:
        # Past settling window (e.g. >10s after origin): fast-forward to steady target at t_origin + (n_ticks - max_ticks)*SIM_DT
        p = target_fn(t_origin + (n_ticks - max_ticks) * SIM_DT)
        start_tick = n_ticks - max_ticks
    else:
        start_tick = 0
    for k in range(start_tick, n_ticks):
        g = target_fn(t_origin + (k + 0.5) * SIM_DT)
        p, v = step_spring(p, v, g, freq_hz, zeta, SIM_DT)
    rem = elapsed - n_ticks * SIM_DT
    if rem > 1e-9:
        p, v = step_spring(p, v, target_fn(t), freq_hz, zeta, rem)
    return p, v


def _decay_rate(freq_hz: float, zeta: float) -> float:
    """Effective exponential decay rate (1/s) when driven by a constant target."""
    w = 2.0 * math.pi * max(1e-4, freq_hz)
    if zeta <= 1.0:
        return w * max(0.01, zeta)
    return w * max(0.01, zeta - math.sqrt(zeta * zeta - 1.0))


class CachedSpringCurve:
    """Incrementally cached 240 Hz spring trajectory anchored at `t_origin`.

    Produces bit-identical `(p, v)` outputs to `eval_spring_240hz` for both linear playback
    and arbitrary seeks while memoizing previously computed ticks in O(1) array slots.
    """

    __slots__ = (
        "target_fn",
        "t_origin",
        "initial",
        "freq_hz",
        "zeta",
        "max_ticks",
        "steady_after",
        "steady_value",
        "_steady_done_t",
        "_a11",
        "_a12",
        "_a21",
        "_a22",
        "_p",
        "_v",
        "_settled_tick",
    )

    def __init__(
        self,
        target_fn: Callable[[float], float],
        t_origin: float,
        initial: float = 0.0,
        freq_hz: float = DEFAULT_FREQ_HZ,
        zeta: float = DEFAULT_ZETA,
        max_ticks: int = 2400,
        steady_after: float | None = None,
        steady_value: float = 0.0,
    ) -> None:
        self.target_fn = target_fn
        self.t_origin = t_origin
        self.initial = initial
        self.freq_hz = freq_hz
        self.zeta = zeta
        self.max_ticks = max_ticks
        self.steady_after = steady_after
        self.steady_value = steady_value
        self._steady_done_t = (
            steady_after + 40.0 / _decay_rate(freq_hz, zeta) if steady_after is not None else None
        )
        self._a11, self._a12, self._a21, self._a22 = _spring_step_coeffs(freq_hz, zeta, SIM_DT)
        self._p = array("d", (initial,))
        self._v = array("d", (0.0,))
        self._settled_tick: int | None = None

    def eval(self, t: float) -> tuple[float, float]:
        if t <= self.t_origin:
            return self.initial, 0.0
        if self._steady_done_t is not None and t >= self._steady_done_t:
            return self.steady_value, 0.0
        elapsed = t - self.t_origin
        n_ticks = int(math.floor(elapsed * SIM_HZ))
        if n_ticks > self.max_ticks:
            return eval_spring_240hz(
                self.target_fn, t, self.t_origin, self.initial, self.freq_hz, self.zeta, self.max_ticks
            )
        if self._settled_tick is not None and n_ticks >= self._settled_tick:
            return self.steady_value, 0.0
        p_arr = self._p
        v_arr = self._v
        cached = len(p_arr) - 1
        if cached < n_ticks:
            p = p_arr[cached]
            v = v_arr[cached]
            target_fn = self.target_fn
            t_origin = self.t_origin
            a11, a12, a21, a22 = self._a11, self._a12, self._a21, self._a22
            steady_after = self.steady_after
            steady_value = self.steady_value
            for k in range(cached, n_ticks):
                tick_t = t_origin + (k + 0.5) * SIM_DT
                g = target_fn(tick_t)
                o = p - g
                if abs(o) < 1e-12 and abs(v) < 1e-12:
                    p, v = g, 0.0
                else:
                    p, v = a11 * o + a12 * v + g, a21 * o + a22 * v
                p_arr.append(p)
                v_arr.append(v)
                if steady_after is not None and tick_t >= steady_after and p == steady_value and v == 0.0:
                    self._settled_tick = k + 1
                    return steady_value, 0.0
        p, v = p_arr[n_ticks], v_arr[n_ticks]
        rem = elapsed - n_ticks * SIM_DT
        if rem > 1e-9:
            p, v = step_spring(p, v, self.target_fn(t), self.freq_hz, self.zeta, rem)
        return p, v
