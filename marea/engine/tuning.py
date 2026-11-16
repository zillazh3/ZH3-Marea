"""Every look-and-feel knob of the lyric renderer in one place. Edit the defaults here; nothing else needs to change.

No imports on purpose: the engine, the scene and the wordfx renderer all read `TUNING`, so it must not depend on any of them.
Changing a default alters the picture (and the frozen golden in tests/fixtures/engine_golden.json: regenerate it with
`python tools/gen_engine_golden.py` after an intentional change). The reference curves in marea/engine/curves.py are NOT here:
they are pinned to the prototype (tests/fixtures/engine_refs.json) and these knobs are applied on top of them.
Lengths are "design px" (a 42 px letter) or em unless stated; the renderer scales them to the output size.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Tuning:
    # ---------------- timing ----------------
    # Seconds BEFORE its sung onset that a line becomes active and a hero word starts to react. One constant for the line, its
    # words and the interlude, so they all agree. 0 = exactly on the beat; above ~0.06 the text visibly moves early.
    anticipation_s: float = 0.05

    # ---------------- scroll (spring physics per line) ----------------
    stagger_ms: float = 50.0  # delay between consecutive lines following the active one: the cascade. 0 = all lines move together
    stagger_decay: float = 1.05  # each line below the active one waits this much less than the previous (1.0 = constant steps)
    scroll_k_base: float = 170.0  # spring stiffness for normal line changes: higher = snappier
    scroll_k_range: float = 50.0  # extra stiffness added when the next line follows quickly (fast songs scroll faster)
    scroll_damping: float = 2.2  # damping = scroll_damping * sqrt(k): ~2 is critically damped, lower bounces, higher feels heavy
    scroll_seek_k: float = 90.0  # stiffness after a seek / over an interlude (slower, calmer)
    scroll_seek_c: float = 15.0  # damping for that case

    # ---------------- active vs inactive lines ----------------
    inactive_scale: float = 0.97  # size of a line that is not being sung (1.0 = same size as the active one)
    line_scale_k: float = 100.0  # stiffness of the line grow/shrink spring
    line_scale_c: float = 12.0  # its damping: 2*sqrt(k) = 20 is critically damped; 10 overshoots ~5 %, 12 barely
    active_opacity: float = 0.85  # opacity of the ACTIVE line as a whole (prototype); inactive lines are 1.0 here and get dimmer via dim_alpha
    dim_alpha: float = 0.2  # brightness of inactive text, and of unsung text on a line that is not active
    unsung_alpha_active: float = 0.4  # brightness of the not-yet-sung part of the ACTIVE line (the wipe goes from this to 1.0)
    blur_per_line: float = 1.0  # blur (design px) added per line of distance from the active line; 0 = no blur at all
    blur_max: float = 5.0  # blur ceiling (design px)

    # ---------------- per-syllable motion ----------------
    # The prototype curves give a ~0.7 design px lift and a 5 % scale pop while a syllable is sung; these scale the lift on top.
    lift_gain: float = 1.4  # multiplier on the upward lift of a syllable (1.0 = prototype, 2.0 = double)
    letter_lift_gain: float = 1.0  # same for the letters of emphasised (long) words; they already pop hard (1.175x scale), so 1.0
    sustain_scale: float = 1.0  # held notes grow this many times more above 1.0
    sustain_lift: float = 2.0  # held notes rise this many times more (with lift_gain 1.4 a held note rises about as much as before)
    sustain_glow: float = 0.6  # held notes glow this much more

    # ---------------- glow (the soft halo behind the sung text) ----------------
    glow_alpha: float = 0.44  # peak halo opacity of a syllable (prototype 0.35)
    glow_radius: float = 4.0  # halo radius at rest (design px)
    glow_radius_gain: float = 3.0  # extra radius at full glow (prototype 2)
    emph_glow_alpha: float = 1.27  # same for letters of emphasised words (prototype ~1.02, capped at 1)
    emph_glow_radius: float = 4.0
    emph_glow_radius_gain: float = 12.0
    glow_peak_at: float = 0.85  # point of the syllable (0..1) where the glow is strongest: later = it builds longer, then settles
    glow_end_level: float = 0.7  # glow level when the syllable ends, before it fades out (1 = no settling dip)
    glow_white_mix: float = 0.6  # 0 = halo in the cover's dominant colour, 1 = pure white; between them it stays tinted but soft
    bloom_gain: float = 1.25  # strength of the soft bloom around the active line (prototype 1.0); 0 = no bloom
    glow_pad_em: float = 0.8  # room around a word for its glow before it is clipped; must exceed 3 x the widest FX glow sigma (marea/wordfx/config.py: 0.645)

    # ---------------- stability / diagnostics ----------------
    # Seconds a line that scrolled out of the active neighbourhood keeps animating, so its last bounce, glow and wave ease to rest
    # instead of snapping. Must cover the slowest spring (~1 s) plus the wave tail (0.7 s).
    tail_settle_s: float = 1.5
    # A scroll step larger than this many font sizes in ONE frame is logged as a jump (logger "marea.engine.engine"). Normal
    # scrolling peaks near 0.25 em/frame; raise it if a very large font logs false positives.
    jump_log_em: float = 0.5


TUNING = Tuning()
