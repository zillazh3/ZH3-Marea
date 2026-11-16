"""Named variants of a category's look. A variant is a set of overrides on top of the category's motion signature, so new looks are
data (a few fields), not new code. Every category implicitly has the `base` variant (no overrides = the look it always had).

Picking is deterministic: `pick()` hashes the word's seed (song + line text + word index), so a chorus that comes back gets the same
variant, and the previous FX word of the line is excluded so two neighbours never look the same when the category offers a choice.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from marea.wordfx import config


@dataclass(frozen=True)
class Variant:
    name: str
    weight: float = 1.0  # relative chance among the variants of its category (config.FX.variant_weights can override per id)
    sig: Mapping[str, object] = field(default_factory=dict)  # MotionSignature fields replaced for this variant (e.g. waveform, axis)


BASE = Variant("base")

def _v(name: str, weight: float = 1.0, **sig) -> Variant:
    return Variant(name, weight, sig)


# category -> its variants (`base` is added automatically when the first one is not named "base").
# `sig` overrides MotionSignature fields; the category's own signature (signatures.py) is the `base` look.
VARIANTS: dict[str, tuple[Variant, ...]] = {
    "sadness": (_v("heavy", waveform="breath", freq_range_hz=(0.3, 0.5), amp_range_em=(0.012, 0.024)), _v("bleed", tint_spread="ink")),
    "joy": (_v("lift", waveform="orbit", axis="y", amp_range_em=(0.020, 0.035)), _v("sparkle", shimmer=0.6, sparkle_style="star")),
    "anguish": (_v("tremor", waveform="sine", axis="x", freq_range_hz=(8.0, 10.0), amp_range_em=(0.006, 0.012)),
                _v("pulse", waveform="heartbeat", axis="scale", freq_range_hz=(1.4, 1.8), amp_range_em=(0.030, 0.045))),
    "fear": (_v("shrink", waveform="erode", axis="scale", amp_range_em=(0.020, 0.040)),
             _v("flutter", waveform="flicker", axis="y", freq_range_hz=(3.0, 4.0))),
    "love": (_v("breathe", waveform="breath", freq_range_hz=(0.7, 1.1)), _v("glow", shimmer=0.5, glow_spread=0.65)),
    "anger": (_v("scatter", scatter_em=0.05), _v("flare", waveform="step", freq_range_hz=(2.2, 3.4), glow_spread=0.7)),
    "fire": (_v("heat", waveform="shimmer", freq_range_hz=(2.2, 3.2)), _v("flare", glow_spread=0.9, shimmer=0.5)),
    "water": (_v("ripple", motif="ripple"), _v("tide", waveform="breath", freq_range_hz=(0.4, 0.7))),
    "air": (_v("gust", waveform="surge"), _v("float", waveform="sine", axis="y", freq_range_hz=(0.3, 0.5))),
    "celestial": (_v("twinkle", shimmer=0.7, sparkle_style="star"), _v("corona", glow_spread=0.9, motif="corona")),
    "shadow": (_v("veil", tint_spread="ink"), _v("ember-core", glow_spread=0.3, shimmer=0.4)),
    "hope": (_v("dawn", shimmer=0.8), _v("rise", axis="y", freq_range_hz=(0.4, 0.7))),
    "loneliness": (_v("drift", waveform="sine", freq_range_hz=(0.2, 0.4)), _v("hollow", waveform="erode", axis="y")),
    "desire": (_v("ember", sparkle_style="ember", glow_spread=0.7), _v("sway", waveform="orbit", axis="x", freq_range_hz=(0.6, 0.9))),
    "freedom": (_v("soar", axis="y", waveform="orbit"), _v("scatter", scatter_em=0.06)),
    "light": (_v("sweep", shimmer=1.0), _v("halo", glow_spread=0.95, motif="corona", shimmer=0.4)),
    "electric": (_v("crackle", glitch=1.0), _v("pulse", waveform="tick", glitch=0.0)),
    "cold": (_v("frost", tint_spread="frost"), _v("shiver", waveform="flicker", freq_range_hz=(2.4, 3.2), tint_spread="")),
    "death": (_v("fade", tint_spread="ink", waveform="erode"), _v("still", waveform="sine", freq_range_hz=(0.2, 0.3), amp_range_em=(0.004, 0.008))),
    "scream": (_v("burst", scatter_em=0.07), _v("tremor", waveform="flicker", freq_range_hz=(6.0, 8.0))),
    "whisper": (_v("hush", waveform="sine", freq_range_hz=(0.3, 0.5)), _v("breathy", waveform="breath", shimmer=0.3)),
    "breath": (_v("inhale", waveform="breath", axis="scale"), _v("sigh", waveform="erode", axis="y")),
}


def options_for(category: str) -> tuple[Variant, ...]:
    """Variants a category offers, `base` first. Categories without entries just have `base`."""
    vs = VARIANTS.get(category, ())
    return vs if vs and vs[0].name == "base" else (BASE, *vs)


def variant_id(category: str, name: str) -> str:
    return f"{category}.{name}"


def weight_of(category: str, v: Variant) -> float:
    return max(0.0, config.FX.variant_weights.get(variant_id(category, v.name), v.weight))


def pick(category: str, u: float, avoid: str = "") -> Variant:
    """Variant of `category` for the uniform `u` in [0, 1), never the one whose id is `avoid` unless it is the only choice."""
    opts: Sequence[Variant] = options_for(category)
    if len(opts) > 1 and avoid:
        rest = tuple(v for v in opts if variant_id(category, v.name) != avoid)
        opts = rest or opts
    ws = [weight_of(category, v) for v in opts]
    total = sum(ws)
    if total <= 0.0:
        return opts[0]
    x = u * total
    for v, w in zip(opts, ws):
        x -= w
        if x < 0.0:
            return v
    return opts[-1]
