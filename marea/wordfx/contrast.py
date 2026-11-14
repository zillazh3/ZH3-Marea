"""Contraste con el fondo: el color de cada palabra se corrige según lo que hay detrás, sin cambiar su tono.

Funciones puras (sin skia ni estado): el mismo (color, fondo) da siempre el mismo resultado, así preview y export coinciden.
Sobre fondo oscuro la palabra se aclara (hacia blanco, como siempre); sobre fondo claro se oscurece conservando tono y saturación.
"""
from dataclasses import replace
from functools import lru_cache

from marea.wordfx.fxtypes import RGB, FxParams

TARGET = 3.0  # razón de contraste mínima (WCAG para texto grande y grueso)
SWITCH, BAND = 0.22, 0.07  # luminancia del fondo en que se pasa de aclarar a oscurecer, y mitad del ancho de la transición
RAMP_HI, RAMP_LO = 3.5, 2.5  # el efecto empieza con este contraste y es completo con el segundo


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(v: float) -> int:
    v = min(1.0, max(0.0, v))
    return round(255 * (12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055))


def luminance(rgb: RGB) -> float:
    return 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])


def contrast(a: RGB, b: RGB) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _smooth(a: float, b: float, x: float) -> float:
    u = min(1.0, max(0.0, (x - a) / (b - a)))
    return u * u * (3.0 - 2.0 * u)


@lru_cache(maxsize=4096)
def adapt(rgb: RGB, bg: RGB) -> RGB:
    """`rgb` corregido hacia `TARGET` frente a `bg`, de forma continua (sin saltos al variar el fondo).

    Tres mezclas suaves en luz lineal: (1) el efecto entra poco a poco al bajar el contraste de `RAMP_HI` a `RAMP_LO`;
    (2) el destino mezcla «aclarar» y «oscurecer» según la luminancia del fondo (banda `SWITCH ± BAND`);
    (3) aclarar es mezclar hacia blanco y oscurecer escalar, así el tono y la saturación se conservan."""
    a = min(1.0, max(0.0, (RAMP_HI - contrast(rgb, bg)) / (RAMP_HI - RAMP_LO)))
    if a <= 0.0:
        return rgb
    y, lb = luminance(rgb), luminance(bg)
    light_y, dark_y = min(1.0, TARGET * (lb + 0.05) - 0.05), max(0.0, (lb + 0.05) / TARGET - 0.05)  # luminancia necesaria a cada lado
    w = _smooth(SWITCH - BAND, SWITCH + BAND, lb)  # 0 = aclarar, 1 = oscurecer
    t = max(0.0, (light_y - y) / (1.0 - y)) if y < 1.0 else 0.0
    k = min(1.0, dark_y / y) if y > 0.0 else 1.0
    out = []
    for c in (_lin(v) for v in rgb):
        target = (1.0 - w) * (c + (1.0 - c) * t) + w * (c * k)
        out.append(_srgb(c + (target - c) * a))
    return tuple(out)


def adapt_params(p: FxParams, bg: RGB | None) -> FxParams:
    """Parámetros con los tres colores de la palabra adaptados al fondo (de ellos derivan letras, glow, polvo y destellos)."""
    if bg is None:
        return p
    cf = adapt(p.color_from, bg)
    ct = adapt(p.color_to, bg)
    co = adapt(p.osc_color, bg)
    if cf == p.color_from and ct == p.color_to and co == p.osc_color:
        return p
    return replace(p, color_from=cf, color_to=ct, osc_color=co)
