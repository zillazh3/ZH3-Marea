"""Shared scalar and color math helpers for WordFX."""
from collections.abc import Sequence

from marea.engine.curves import clamp, smoothstep_range as smoothstep
from marea.wordfx.fxtypes import RGB

MAX_AMBIENT_AMP = 0.15

_WARM_POS: RGB = (255, 198, 128)  # warm luminous amber
_COOL_POS: RGB = (164, 224, 255)  # airy sky cyan
_WARM_NEG: RGB = (232, 134, 152)  # tender bruised rose
_COOL_NEG: RGB = (122, 168, 238)  # deep nostalgic indigo-blue


def lerp_rgb(a: RGB, b: RGB, u: float) -> RGB:
    """Linearly interpolate two RGB triples with `u` clamped to `[0, 1]`."""
    if a == b or u <= 0.0:
        return a
    if u >= 1.0:
        return b
    return (
        round(a[0] + (b[0] - a[0]) * u),
        round(a[1] + (b[1] - a[1]) * u),
        round(a[2] + (b[2] - a[2]) * u),
    )


def luma(rgb: RGB) -> float:
    """Relative sRGB luma in `[0, 1]`."""
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def ambient_vector_color(vec: Sequence[float]) -> RGB:
    """Map a 6-axis Undertow vector cleanly to a cohesive ambient tint RGB."""
    v = vec[0] if len(vec) > 0 else 0.0
    a = vec[1] if len(vec) > 1 else 0.0
    t = vec[3] if len(vec) > 3 else 0.0
    wu = clamp(0.5 + 0.5 * t, 0.0, 1.0)
    vu = clamp(0.5 + 0.5 * v, 0.0, 1.0)
    neg = lerp_rgb(_COOL_NEG, _WARM_NEG, wu)
    pos = lerp_rgb(_COOL_POS, _WARM_POS, wu)
    base = lerp_rgb(neg, pos, vu)
    # High arousal nudges slightly brighter so energy reads naturally in tint
    return lerp_rgb(base, (255, 244, 228), clamp(0.15 * max(0.0, a), 0.0, 0.15))
