"""Every tunable of the semantic word FX in one place. Edit the defaults here; nothing else needs to change.

No imports from the rest of marea on purpose (engine, composer, envelope, anim and render all read it).
Read it as `config.FX.<field>` (module attribute, not `from ... import FX`) so `set_fx()` (used by the FX Gallery) takes effect.
Changing a default alters the picture; the wordfx tests pin the shipped values. Lengths are em (1 em = the font size) unless stated.
Line-level look (scroll, bloom, dimming, base glow) lives in marea/engine/tuning.py; this file is only for the word FX layer.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class FxConfig:
    # ---------------- density ----------------
    max_fx_per_line: int = 2  # words per line that get an FX (the highest-scoring ones); the rest keep the plain Apple Music look

    max_strong_per_line: int = 1  # of those, how many are "strong" (full FX); the rest are "light": quieter, no particles / streak / chroma
    light_scale: float = 0.55  # amplitude of a light word relative to a strong one
    variant_weights: Mapping[str, float] = field(default_factory=dict)  # "category.variant" -> relative chance (default: the variant's own weight)

    # ---------------- envelope: anticipation -> attack -> sustain -> release ----------------
    anticipation_amp: float = 0.13  # amplitude of the tiny pre-onset reaction (hero words only); its length is TUNING.anticipation_s
    accent_max_s: float = 0.25  # words shorter than this are an "accent": a quick small hit
    held_min_s: float = 0.70  # words longer than this are "held": a slow build that grows with log(duration)
    accent_amp: float = 0.40  # peak of an accent word (1.0 = a normal word)
    held_amp_cap: float = 1.85  # ceiling of the held build
    min_release_s: float = 0.40  # a category's release is clamped to this range (seconds)
    max_release_s: float = 0.90
    min_residue: float = 0.10  # a category's residue is clamped to this range ...
    max_residue: float = 0.25
    residue_gain: float = 0.0  # ... then multiplied by this: share of it kept after the release. 0 = the word returns to the plain Apple Music look
    past_line_tint: float = 0.0  # colour tint left on a word whose line is over (0 = none)

    # ---------------- motion clamps (em): the most a semantic effect may displace a letter in the carrier path ----------------
    drift_max_em: float = 0.04  # slow vertical drift (sadness sinks, joy lifts)
    wave_max_em: float = 0.035  # legacy wave amplitude
    scale_amp_max: float = 0.06  # breathing / pulse scale amplitude
    rotation_max_deg: float = 4.0  # per-letter tilt
    offset_max_em: float = 0.05  # total semantic displacement per axis
    offset_max_held_em: float = 0.08  # same for a held hero word

    # ---------------- 12 principles, scaled down to text ----------------
    stretch_amt: float = 0.03  # squash & stretch on attack: letters stretch then squash by up to this share, area kept (0 = off)
    stretch_s: float = 0.18  # length of that attack window (seconds)
    anticipation_dip_em: float = 0.02  # tiny counter-motion right before the main move (about 1 px at 1080p); 0 = off

    # ---------------- timing of the legacy (non-carrier) letter path and particles ----------------
    enter_s: float = 0.12  # per-letter fade-in
    erode_tail: float = 0.4  # last share of the sung interval in which eroded letters dissolve
    vanish_s: float = 1.0  # dissolve length once the next line starts
    drift_tail_s: float = 0.15  # drift keeps going this long after the word ends
    echo_hz: float = 0.35  # oscillation of the trail copies
    gravity: float = 1.2  # em/s^2 for dust particles

    # ---------------- glow of FX words ----------------
    glow_gain: float = 1.25  # strength of the FX glow (1.0 = prototype)
    glow_core_white: float = 0.5  # how far the bright core of the glow is pulled toward white (0 = full FX colour)
    glow_sigmas: tuple[float, float, float] = (0.085, 0.135, 0.215)  # tight / standard / wide halo tiers (em); TUNING.glow_pad_em must exceed 3x the widest
    glow_by_category: Mapping[str, float] = field(default_factory=lambda: {
        # bright / energetic: noticeably brighter (+30..50 %)
        "celestial": 1.5, "light": 1.5, "fire": 1.4, "electric": 1.45, "joy": 1.35, "love": 1.3, "hope": 1.35, "summer": 1.3,
        "healing": 1.15, "tenderness": 1.1,
        # calm / dark: at or below the base glow
        "sadness": 0.85, "loneliness": 0.8, "shadow": 0.7, "fear": 0.75, "death": 0.7, "numbness": 0.6, "loss": 0.85})  # unlisted = 1.0
    glow_build_start: float = 0.55  # glow level at the word's onset; it builds to 1.0 at the word's end, then the release settles it
    glow_bright_floor: float = 0.6  # on a fully bright background the glow drops to this share so the text stays legible
    glow_asc: float = 1.0  # em above / below the baseline covered by a glow sprite
    glow_desc: float = 0.4

    # ---------------- micro light streak: a thin anamorphic line through the middle of the word, anchored to the fill head ----------------
    streak_thickness_px: float = 1.0  # at 1080p; scaled with the output size
    streak_opacity: float = 0.30  # peak alpha at the fill head before the per-category strength (keep it barely noticeable)
    streak_front_em: float = 0.30  # length of the short segment in front of the head (tapers to 0)
    streak_trail_em: float = 1.40  # length of the trail behind the head
    streak_decay: float = 2.2  # how fast the trail fades (higher = dissolves sooner)
    streak_overshoot_em: float = 0.15  # how far it may extend past the word's left/right edges
    streak_white_mix: float = 0.85  # 1 = pure white core, lower lets more of the FX colour through
    streak_by_category: Mapping[str, float] = field(default_factory=lambda: {
        "celestial": 1.0, "light": 1.0, "electric": 1.0, "fire": 0.85, "love": 0.7, "joy": 0.8, "hope": 0.7, "summer": 0.7,
        "healing": 0.5, "tenderness": 0.45, "dream": 0.4, "sadness": 0.15, "fear": 0.0, "shadow": 0.0, "loneliness": 0.1})  # unlisted = streak_default
    streak_default: float = 0.0  # strength for categories not listed above (0 = off)

    # ---------------- render ----------------
    ghosts: tuple[tuple[float, float], ...] = ((0.12, 0.1), (0.08, 0.2), (0.04, 0.35))  # trail copies: (delay s, base alpha), oldest first
    fracture_gap: float = 0.08  # final gap between the two halves of a fractured letter (em)
    fracture_rot_deg: float = 4.0  # tilt of each half at fracture = 1


FX = FxConfig()


def set_fx(cfg: FxConfig) -> FxConfig:
    """Replace the active config (FX Gallery, tests). Returns the previous one so callers can restore it."""
    global FX
    prev, FX = FX, cfg
    return prev


def with_fx(**changes) -> FxConfig:
    """A copy of the active config with some fields changed (does not activate it)."""
    return replace(FX, **changes)
