"""Springs del prototipo: `Spring` (k/c, AMLL) y `Spr` analítico por frecuencia y ratio (Spicy Lyrics)."""
import math


class Spring:
    def __init__(self, p):
        self.p = p
        self.v = 0.0
        self.t = p
        self.k = 100.0
        self.c = 10.0
        self.pending = None  # (meta, instante en ms): retraso del stagger

    def to(self, target, delay_ms, now):
        if delay_ms > 0:
            self.pending = (target, now + delay_ms)
        else:
            self.t = target
            self.pending = None

    def snap(self, p):
        self.p = p
        self.t = p
        self.v = 0.0
        self.pending = None

    def step(self, dt, now):
        if self.pending and now >= self.pending[1]:
            self.t = self.pending[0]
            self.pending = None
        n = 4
        h = dt / n
        for _ in range(n):
            a = -self.k * (self.p - self.t) - self.c * self.v
            self.v += a * h
            self.p += self.v * h
        if abs(self.p - self.t) < 0.01 and abs(self.v) < 0.01:
            self.p = self.t
            self.v = 0.0
        return self.p


def step_spring(p: float, v: float, g: float, freq_hz: float, zeta: float, dt: float) -> tuple[float, float]:
    """Advance spring state `(p, v)` toward target `g` by `dt` seconds using the analytical solution.

    Zero numerical drift and unconditionally stable for any `dt >= 0`.
    """
    if dt <= 0.0 or freq_hz <= 0.0:
        return p, v
    w = freq_hz * 2.0 * math.pi
    o = p - g
    if abs(o) < 1e-12 and abs(v) < 1e-12:
        return g, 0.0
    if abs(zeta - 1.0) < 1e-6:
        q = math.exp(-w * dt)
        t_q = dt * q
        np = o * (q + t_q * w) + v * t_q + g
        nv = v * (q - t_q * w) - o * (t_q * w * w)
    elif zeta < 1.0:
        z = max(0.01, zeta)
        q = math.exp(-z * w * dt)
        c = math.sqrt(1.0 - z * z)
        cs = math.cos(dt * w * c)
        sn = math.sin(dt * w * c)
        k1 = sn / c
        k2 = sn / (w * c)
        np = (o * (cs + k1 * z) + v * k2) * q + g
        nv = (v * (cs - k1 * z) - o * (k1 * w)) * q
    else:
        c = math.sqrt(zeta * zeta - 1.0)
        r1 = -w * (zeta + c)
        r2 = -w * (zeta - c)
        e1 = math.exp(r1 * dt)
        e2 = math.exp(r2 * dt)
        c2 = (v - o * r1) / (2.0 * w * c)
        c1 = e1 * (o - c2)
        np = c1 + c2 * e2 + g
        nv = c1 * r1 + c2 * e2 * r2
    return np, nv


class Spr:
    """Port de `spr` (Fraktality): solución analítica, estable con cualquier dt."""

    def __init__(self, p, f, d):
        self.p = p
        self.v = 0.0
        self.g = p
        self.f = f
        self.d = d

    def snap(self, p):
        self.p = p
        self.g = p
        self.v = 0.0

    def step(self, dt):
        self.p, self.v = step_spring(self.p, self.v, self.g, self.f, self.d, dt)
        return self.p
