"""Curvas del prototipo: bezier CSS, spline cúbica natural (como `cubic-spline`) y las curvas de Spicy Lyrics."""


def clamp01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def bezier(x1, y1, x2, y2):
    cx = 3 * x1
    bx = 3 * (x2 - x1) - cx
    ax = 1 - cx - bx
    cy = 3 * y1
    by = 3 * (y2 - y1) - cy
    ay = 1 - cy - by

    def f(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        t = x
        for _ in range(8):
            e = ((ax * t + bx) * t + cx) * t - x
            d = (3 * ax * t + 2 * bx) * t + cx
            if abs(e) < 1e-6 or abs(d) < 1e-6:
                break
            t -= e / d
        return ((ay * clamp01(t) + by) * clamp01(t) + cy) * clamp01(t)

    return f


EASE = bezier(0.25, 0.1, 0.25, 1)  # CSS `ease`
EASE_OUT = bezier(0, 0, 0.58, 1)  # CSS `ease-out`


def spline(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs) - 1
    h = [xs[i + 1] - xs[i] for i in range(n)]
    al, l, mu, z, c = [0.0] * (n + 1), [1.0] * (n + 1), [0.0] * (n + 1), [0.0] * (n + 1), [0.0] * (n + 1)
    b, d = [0.0] * n, [0.0] * n
    for i in range(1, n):
        al[i] = (3 / h[i]) * (ys[i + 1] - ys[i]) - (3 / h[i - 1]) * (ys[i] - ys[i - 1])
    for i in range(1, n):
        l[i] = 2 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        mu[i] = h[i] / l[i]
        z[i] = (al[i] - h[i - 1] * z[i - 1]) / l[i]
    for j in range(n - 1, -1, -1):
        c[j] = z[j] - mu[j] * c[j + 1]
        b[j] = (ys[j + 1] - ys[j]) / h[j] - (h[j] * (c[j + 1] + 2 * c[j])) / 3
        d[j] = (c[j + 1] - c[j]) / (3 * h[j])

    def f(x):
        x = clamp01(x)
        i = n - 1
        for k in range(n):
            if x <= xs[k + 1]:
                i = k
                break
        dx = x - xs[i]
        return ys[i] + b[i] * dx + c[i] * dx * dx + d[i] * dx * dx * dx

    return f


# Avance del wipe dentro de una sílaba: arranca y termina a ~40 % de la velocidad media y pasa de 1.4× en el centro. Sin esto el
# relleno es lineal y las sílabas seguidas se leen como un solo barrido; con esto cada una «respira». Calibrable (0.3,0.12,0.7,0.88).
WIPE = bezier(0.3, 0.12, 0.7, 0.88)
WIPE_INV = bezier(0.12, 0.3, 0.88, 0.7)  # inversa de WIPE (puntos de control transpuestos): progreso del wipe -> fracción del tiempo


def smoothstep(x):
    x = clamp01(x)
    return x * x * (3 - 2 * x)


# Progreso de la sílaba 0→1 (Spicy Lyrics)
SCALE = spline([(0, 0.95), (0.7, 1.0505), (1, 1)])
YOFF = spline([(0, 0.01), (0.9, -1 / 60), (1, 0)])
GLOW = spline([(0, 0), (0.15, 1), (0.6, 1), (1, 0)])
# Progreso de la letra activa en palabras con énfasis
L_SCALE = spline([(0, 0.95), (0.7, 1.175), (1, 1)])
L_YOFF = spline([(0, 0.01), (0.9, -1 / 56), (1, 0)])
