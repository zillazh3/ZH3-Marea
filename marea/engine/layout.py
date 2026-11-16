"""Reglas de layout de AMLL: spring de scroll, stagger y blur por distancia."""
import math

from marea.engine.tuning import TUNING

BLUR_MAX = TUNING.blur_max
INACTIVE_SCALE = TUNING.inactive_scale


def pos_y_policy(seek, interlude, interval_ms):
    """Constantes (k, c) del spring Y de cada línea (AMLL getPosYSpringPolicy)."""
    if seek or interlude or interval_ms is None:
        return TUNING.scroll_seek_k, TUNING.scroll_seek_c
    ci = min(max(interval_ms, 100), 800)
    ratio = (1 - (ci - 100) / 700) ** 0.2
    k = TUNING.scroll_k_base + ratio * TUNING.scroll_k_range
    return k, math.sqrt(k) * TUNING.scroll_damping


def blur_level(i, active, blurm=1.0, scale=1.0):
    """Blur (px a tamaño de diseño) de la línea i; la activa no lleva."""
    if i == active or blurm <= 0:
        return 0.0
    dist = active - i + 1 if i < active else i - active
    return min(BLUR_MAX, (1 + dist) * scale * blurm * TUNING.blur_per_line)
