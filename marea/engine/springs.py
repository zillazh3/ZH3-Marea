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
        d = self.d
        f = self.f * 2 * math.pi
        g = self.g
        o = self.p - g
        v = self.v
        if d == 1:
            q = math.exp(-f * dt)
            w = dt * q
            p = o * (q + w * f) + v * w + g
            v = v * (q - w * f) - o * (w * f * f)
        elif d < 1:
            q = math.exp(-d * f * dt)
            c = math.sqrt(1 - d * d)
            i = math.cos(dt * f * c)
            j = math.sin(dt * f * c)
            z = j / c
            y = j / (f * c)
            p = (o * (i + z * d) + v * y) * q + g
            v = (v * (i - z * d) - o * (z * f)) * q
        else:
            c = math.sqrt(d * d - 1)
            r1 = -f * (d + c)
            r2 = -f * (d - c)
            e1 = math.exp(r1 * dt)
            e2 = math.exp(r2 * dt)
            co2 = (v - o * r1) / (2 * f * c)
            co1 = e1 * (o - co2)
            p = co1 + co2 * e2 + g
            v = co1 * r1 + co2 * e2 * r2
        self.p = p
        self.v = v
        return p
