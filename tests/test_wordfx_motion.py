"""Word FX v2: staging, motion principles, per-category glow, and the micro light streak."""
import math
from dataclasses import replace

import pytest
import skia

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.text import Fonts
from marea.wordfx import config, library
from marea.wordfx.anim import letter_state
from marea.wordfx.composer import compose
from marea.wordfx.fxtypes import CarrierLetter, FxParams, FxWord
from marea.wordfx.matcher import Match
from marea.wordfx.plan import build_plan
from marea.wordfx.render import draw_word_fx, draw_word_streak, letters_for, streak_alpha, streak_geometry

B, E = 1.0, 2.0  # sung interval (s)


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


@pytest.fixture
def restore_fx():
    prev = config.FX
    yield
    config.set_fx(prev)


def line_of(text: str, start=1000, per_word=500) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def M(lemma, cat) -> Match:
    return Match(0, 0, lemma, lemma, "es", (cat,), 1000, 2200)


def P(**kw) -> FxParams:
    return FxParams(seed=7, vector=(0.0,) * 6, primitives=(), color_from=(255, 200, 100), color_to=(255, 200, 100), **kw)


# ---------------- staging ----------------

def test_one_strong_word_per_line_the_rest_light(lib):
    plan = build_plan(Lyrics([line_of("llorar fuego")]), lib, track_id="t")
    tiers = sorted(plan.get(0, w).params.tier for w in (0, 1))
    assert tiers == ["light", "strong"]
    light = next(plan.get(0, w).params for w in (0, 1) if plan.get(0, w).params.tier == "light")
    assert light.particles == light.sparkle == 0 and light.chromatic_em == 0.0 and light.streak == 0.0 and not light.is_hero
    strong = next(plan.get(0, w).params for w in (0, 1) if plan.get(0, w).params.tier == "strong")
    assert strong.is_hero


def test_max_strong_is_configurable(lib, restore_fx):
    config.set_fx(config.with_fx(max_strong_per_line=2))
    plan = build_plan(Lyrics([line_of("llorar fuego")]), lib, track_id="t")
    assert {plan.get(0, w).params.tier for w in (0, 1)} == {"strong"}


def test_light_word_is_quieter(lib):
    strong = compose(M("llorar", "sadness"), lib, track_id="t")
    light = compose(M("llorar", "sadness"), lib, track_id="t", light=True)
    assert abs(light.drift_em) < abs(strong.drift_em) and light.glow < strong.glow


# ---------------- glow per category ----------------

def test_glow_gain_per_category(lib):
    g = {c: compose(M("x", c), lib, track_id="t").glow_gain for c in ("celestial", "fire", "sadness", "shadow", "water")}
    assert g["celestial"] >= 1.3 and g["fire"] >= 1.3  # bright: +30..50 %
    assert g["sadness"] <= 1.0 and g["shadow"] <= 1.0  # calm / dark: at or below the base glow
    assert g["water"] == 1.0  # unlisted
    assert compose(M("x", "celestial"), lib, track_id="t", light=True).glow_gain <= 1.0  # a light word never out-glows the base


# ---------------- micro light streak: strength, geometry, lifecycle ----------------

def test_streak_strength_per_category(lib):
    s = {c: compose(M("x", c), lib, track_id="t").streak for c in ("celestial", "electric", "sadness", "fear", "water")}
    assert s["celestial"] > s["sadness"] > 0.0 and s["fear"] == 0.0 and s["water"] == 0.0  # unlisted = off
    assert compose(M("x", "celestial"), lib, track_id="t", light=True).streak == 0.0


def test_streak_geometry_rides_the_fill_head_and_stays_near_the_word():
    size, x, w = 60.0, 100.0, 200.0
    cfg = config.FX
    for front in (0.0, 0.3, 0.7, 1.0):
        tail, head, tip = streak_geometry(x, w, size, front)
        assert head == pytest.approx(x + front * w)
        assert tail <= head <= tip
        assert tail >= x - cfg.streak_overshoot_em * size - 1e-9 and tip <= x + w + cfg.streak_overshoot_em * size + 1e-9
    tail, head, tip = streak_geometry(x, w, size, 0.5)
    assert head - tail > tip - head  # the trail behind is longer than the taper in front
    assert streak_geometry(x, w, size, -3.0)[1] == x and streak_geometry(x, w, size, 9.0)[1] == x + w


def test_streak_lifecycle_appears_with_attack_and_fades_out_in_release():
    p = P(release_s=0.6)
    assert streak_alpha(p, B - 1.0, B, E) == 0.0
    assert 0.9 < streak_alpha(p, (B + E) / 2, B, E) <= 1.0
    assert 0.0 < streak_alpha(p, E + 0.3, B, E) < 1.0
    assert streak_alpha(p, E + 0.6, B, E) == 0.0 and streak_alpha(p, E + 5.0, B, E) == 0.0


def _draw_streak(p: FxParams, t: float, front_u: float, size=60.0, **kw) -> skia.Image:
    m = Match(0, 0, "Perdido", "perdido", "es", ("x",), int(B * 1000), int(E * 1000))
    surf = skia.Surface(540, 200)
    surf.getCanvas().clear(skia.ColorTRANSPARENT)
    draw_word_streak(surf.getCanvas(), FxWord(m, p), 60.0, 120.0, 300.0, size, t, front_u, **kw)
    return surf.makeImageSnapshot()


def _lit_rows(img: skia.Image) -> list[int]:
    px = img.toarray()
    return [y for y in range(px.shape[0]) if px[y, :, 3].max() > 0]


def test_streak_draws_a_thin_line_through_the_middle_of_the_letters_not_under_them():
    p = P(streak=1.0, release_s=0.6)
    rows = _lit_rows(_draw_streak(p, 1.5, 0.5))
    assert rows and len(rows) <= 6  # thin
    y_mid = 120.0 - 0.26 * 60.0  # the x-height middle, above the baseline (y=120)
    assert all(abs(y - y_mid) <= 4 for y in rows)


def test_streak_off_when_strength_zero_or_after_release_and_never_in_the_unsung_dark():
    assert not _lit_rows(_draw_streak(P(streak=0.0, release_s=0.6), 1.5, 0.5))
    assert not _lit_rows(_draw_streak(P(streak=1.0, release_s=0.6), E + 1.0, 1.0))
    assert not _lit_rows(_draw_streak(P(streak=1.0, release_s=0.6), 1.5, 0.5, wipe_alpha=0.0))


def test_streak_thickness_scales_with_resolution():
    p = P(streak=1.0, release_s=0.6)
    assert len(_lit_rows(_draw_streak(p, 1.5, 0.5, res_scale=4.0))) > len(_lit_rows(_draw_streak(p, 1.5, 0.5, res_scale=1.0)))


# ---------------- 12 principles: squash & stretch, anticipation, return to base ----------------

def _carrier(on=B, fill=1.0) -> CarrierLetter:
    return CarrierLetter(fill_p=fill, onset_s=on)


def test_stretch_keeps_volume_is_bounded_and_returns_to_zero():
    p = P(sig_amp_em=0.02, release_s=0.6)
    peak = 0.0
    for k in range(0, 40):
        t = B + k * 0.01
        st = letter_state(p, 0, 3, t, B, E, carrier=_carrier(), line_end_s=E + 3.0)
        peak = max(peak, abs(st.stretch))
        assert abs(st.stretch) <= config.FX.stretch_amt + 1e-9
    assert 0.0 < peak <= config.FX.stretch_amt
    for t in (B - 0.2, B + config.FX.stretch_s + 0.01, B + 1.0, E + 3.0):
        assert letter_state(p, 0, 3, t, B, E, carrier=_carrier(), line_end_s=E + 3.0).stretch == 0.0


def test_stretch_only_on_strong_and_light_tiers():
    t = B + config.FX.stretch_s * 0.25
    strong = letter_state(P(tier="strong"), 0, 3, t, B, E, carrier=_carrier(), line_end_s=E + 3.0)
    light = letter_state(P(tier="light"), 0, 3, t, B, E, carrier=_carrier(), line_end_s=E + 3.0)
    ambient = letter_state(P(tier="ambient"), 0, 3, t, B, E, carrier=_carrier(), line_end_s=E + 3.0)
    assert abs(strong.stretch) > abs(light.stretch) > 0.0 and ambient.stretch == 0.0


def test_anticipation_dip_is_tiny_opposite_to_the_drift_and_only_for_hero_words():
    from marea.engine.tuning import TUNING
    t = B - TUNING.anticipation_s / 2
    down = letter_state(P(drift_em=0.2), 0, 3, t, B, E, carrier=_carrier(fill=0.0), line_end_s=E + 3.0)
    up = letter_state(P(drift_em=-0.2), 0, 3, t, B, E, carrier=_carrier(fill=0.0), line_end_s=E + 3.0)
    assert down.dy < 0.0 < up.dy and abs(up.dy) <= config.FX.anticipation_dip_em + 1e-9  # a sinking word dips up first
    light = letter_state(P(drift_em=0.2, tier="light"), 0, 3, t, B, E, carrier=_carrier(fill=0.0), line_end_s=E + 3.0)
    assert abs(light.dy) < abs(down.dy)


@pytest.mark.parametrize("cat", ["sadness", "celestial", "fire", "anger", "water", "air", "love", "fear"])
def test_word_is_back_in_the_standard_state_after_the_release(lib, cat):
    p = compose(M("x", cat), lib, track_id="t")
    t = E + p.release_s + 1.5  # well past the release, the line still active
    st = letter_state(p, 1, 3, t, B, E, carrier=_carrier(), line_act=1.0, line_end_s=t + 5.0)
    assert (st.dx, st.dy, st.rot_deg, st.stretch) == pytest.approx((0.0, 0.0, 0.0, 0.0), abs=1e-6)
    assert st.scale == pytest.approx(1.0, abs=1e-6) and st.alpha == pytest.approx(1.0) and st.color == (255, 255, 255)


def test_fast_flicker_only_inside_the_sustain():
    p = P(waveform="flicker", sig_axis="x", sig_amp_em=0.02, sig_freq_hz=3.0, release_s=0.6)
    before = [letter_state(p, 0, 3, B - 0.5 + 0.01 * k, B, E, carrier=_carrier(fill=0.0), line_end_s=E + 5.0).dx for k in range(30)]
    after = [letter_state(p, 0, 3, E + 1.6 + 0.01 * k, B, E, carrier=_carrier(), line_end_s=E + 5.0).dx for k in range(30)]
    assert all(abs(v) < 1e-6 for v in before + after)


def test_glow_builds_toward_the_end_of_the_word():
    fonts = Fonts()
    m = Match(0, 0, "Luz", "luz", "es", ("celestial",), int(B * 1000), int(E * 1000))
    fx = FxWord(m, P(glow=0.8, glow_spread=0.3, glow_gain=1.0))

    def glow_ink(t):
        surf = skia.Surface(300, 160)
        surf.getCanvas().clear(skia.ColorTRANSPARENT)
        draw_word_fx(surf.getCanvas(), fonts, fx, letters_for(fonts, "Luz", 60.0), 60, 110, 60.0, t)
        return float(surf.makeImageSnapshot().toarray()[:, :, 3].astype("float64").sum())

    assert glow_ink(B + 0.05) < glow_ink(E - 0.05)


def test_glow_is_lower_on_a_bright_background():
    fonts = Fonts()
    m = Match(0, 0, "Luz", "luz", "es", ("celestial",), int(B * 1000), int(E * 1000))
    fx = FxWord(m, P(glow=0.8, glow_spread=0.3, glow_gain=1.0))

    def ink(backdrop):
        surf = skia.Surface(300, 160)
        surf.getCanvas().clear(skia.ColorTRANSPARENT)
        draw_word_fx(surf.getCanvas(), fonts, fx, letters_for(fonts, "Luz", 60.0), 60, 110, 60.0, 1.9, backdrop=backdrop)
        return float(surf.makeImageSnapshot().toarray()[:, :, 3].astype("float64").sum())

    assert ink((250, 250, 250)) < ink((10, 10, 30))


# ---------------- T8: new primitives ----------------

def _states(p: FxParams, times, i=0, n=4, fill=1.0):
    return [letter_state(p, i, n, t, B, E, carrier=_carrier(fill=fill), line_act=1.0, line_end_s=E + 6.0) for t in times]


def test_heartbeat_has_at_most_two_beats_from_the_onset_then_rests():
    from marea.wordfx.signatures import eval_waveform
    vals = [eval_waveform("heartbeat", B + k * 0.01, 1.2, 0, age=k * 0.01) for k in range(300)]
    peaks = sum(1 for k in range(1, 299) if vals[k] > 0.9 and vals[k] >= vals[k - 1] and vals[k] > vals[k + 1])
    assert 1 <= peaks <= 2 and eval_waveform("heartbeat", 9.0, 1.2, 0, age=-0.2) == 0.0 and eval_waveform("heartbeat", 9.0, 1.2, 0, age=3.0) == 0.0


def test_shake_dies_away_on_its_own():
    from marea.wordfx.signatures import eval_waveform
    early = max(abs(eval_waveform("shake", 0, 8.0, 0, 0.3, age=a / 100)) for a in range(0, 8))
    late = max(abs(eval_waveform("shake", 0, 8.0, 0, 0.3, age=a / 100)) for a in range(100, 130))
    assert early > 0.5 and late < 0.01


def test_arc_axis_moves_sideways_and_rises_a_little():
    p = P(waveform="sine", sig_axis="arc", sig_amp_em=0.03, sig_freq_hz=0.8, release_s=0.6)
    sts = _states(p, [B + 0.3 + 0.05 * k for k in range(20)])
    assert any(abs(s.dx) > 0.004 for s in sts) and all(s.dy <= 1e-9 for s in sts) and all(abs(s.dx) <= config.FX.offset_max_em + 1e-9 for s in sts)


def test_frost_spreads_left_to_right_and_ink_from_the_centre():
    def tint(mode, i, t):
        return sum(255 - c for c in letter_state(P(tint_spread=mode, fill_tint=0.5, tier="strong"), i, 5, t, B, E,
                                                 carrier=_carrier(), line_act=1.0, line_end_s=E + 6.0).color)
    t = B + 0.25
    assert tint("frost", 0, t) > tint("frost", 4, t)  # the first letter is already tinted when the last one is not
    assert tint("ink", 2, t) > tint("ink", 0, t) and tint("ink", 2, t) > tint("ink", 4, t)  # the centre first
    assert tint("frost", 4, E) > 0  # and the whole word ends up tinted


def test_scatter_reassembles_and_stays_small():
    p = P(scatter_em=0.05, sig_amp_em=0.0, release_s=0.6, tier="strong")  # no other motion: only the scatter
    mid = _states(p, [B + 0.2], i=1)[0]
    assert 0.0 < max(abs(mid.dx), abs(mid.dy)) <= 0.05 * 1.4 + 1e-9
    after = _states(p, [B + 0.45, B + 1.0], i=1)
    assert all(abs(s.dx) < 1e-6 and abs(s.dy) < 1e-6 for s in after)


def test_glitch_is_rare_soft_sustain_only_and_strong_only():
    p = P(glitch=1.0, release_s=0.6, tier="strong")
    sustain = [letter_state(p, i % 4, 4, B + 0.5 + 0.02 * k, B, E, carrier=_carrier(), line_end_s=E + 6.0).dx
               for i in range(4) for k in range(40)]
    assert 0 < sum(1 for v in sustain if abs(v) > 0.015) <= 0.30 * len(sustain) and max(abs(v) for v in sustain) <= 0.03
    assert all(abs(v) < 1e-6 for v in (s.dx for s in _states(p, [E + 2.5, E + 3.0])))  # nothing after the release
    light = [letter_state(replace(p, tier="light"), 0, 4, B + 0.5 + 0.02 * k, B, E, carrier=_carrier(), line_end_s=E + 6.0).dx for k in range(60)]
    assert all(abs(v) < 0.015 for v in light)


def test_shimmer_lights_the_letters_the_fill_is_passing_through():
    base = P(glow=0.3, release_s=0.6, tier="strong")
    lit = replace(base, shimmer=1.0)
    t = B + 0.5
    mid = letter_state(lit, 0, 3, t, B, E, carrier=_carrier(fill=0.5), line_end_s=E + 6.0).glow
    plain = letter_state(base, 0, 3, t, B, E, carrier=_carrier(fill=0.5), line_end_s=E + 6.0).glow
    done = letter_state(lit, 0, 3, t, B, E, carrier=_carrier(fill=1.0), line_end_s=E + 6.0).glow
    assert mid > plain and done == pytest.approx(letter_state(base, 0, 3, t, B, E, carrier=_carrier(fill=1.0), line_end_s=E + 6.0).glow)


def test_ripple_underline_is_drawn_under_the_word_not_through_it():
    from marea.wordfx.render import _draw_motif
    surf = skia.Surface(400, 200)
    surf.getCanvas().clear(skia.ColorTRANSPARENT)
    _draw_motif(surf.getCanvas(), P(motif="ripple", release_s=0.6), 50.0, 120.0, 250.0, 60.0, 1.6, B, E, 1.0, None, 1.0, False)
    rows = _lit_rows(surf.makeImageSnapshot())
    assert rows and min(rows) > 120.0 and max(rows) < 120.0 + 0.4 * 60.0  # just below the baseline


# ---------------- T9/T10: catalog integrity ----------------

def test_every_variant_only_overrides_real_signature_fields_and_every_category_has_a_look():
    import dataclasses
    from marea.wordfx.signatures import SIGNATURES, MotionSignature
    fields = {f.name for f in dataclasses.fields(MotionSignature)}
    from marea.wordfx import variants as V
    lib_cats = set(library.load(library.BUNDLED).categories)
    assert lib_cats <= set(SIGNATURES), sorted(lib_cats - set(SIGNATURES))
    for cat, vs in V.VARIANTS.items():
        assert cat in SIGNATURES and 2 <= len(V.options_for(cat)) <= 3, cat
        assert len({v.name for v in vs}) == len(vs)
        for v in vs:
            assert set(v.sig) <= fields, (cat, v.name)


def test_every_variant_of_every_category_produces_finite_bounded_motion(lib, restore_fx):
    from marea.wordfx import variants as V
    for cat in V.VARIANTS:
        for v in V.options_for(cat):
            config.set_fx(config.with_fx(variant_weights={variant_id: (1.0 if variant_id == f"{cat}.{v.name}" else 0.0)
                                                          for variant_id in (f"{cat}.{o.name}" for o in V.options_for(cat))}))
            p = compose(M("x", cat), lib, track_id="t")
            assert p.variant == f"{cat}.{v.name}"
            for st in _states(p, [B - 0.03, B + 0.05, B + 0.4, E, E + 0.5, E + 3.0], n=5):
                vals = (st.dx, st.dy, st.scale, st.rot_deg, st.alpha, st.glow, st.stretch)
                assert all(math.isfinite(x) for x in vals)
                assert abs(st.dx) <= 0.16 and abs(st.dy) <= 0.16 and 0.8 <= st.scale <= 1.2 and 0.0 <= st.alpha <= 1.0


# ---------------- scene integration ----------------

def _scene_frame(lib, categories=("celestial",), frac=0.6) -> bytes:
    from marea.demo_cover import demo_cover
    from marea.render.scene import Scene, Style
    from marea.wordfx import songs
    lyr = Lyrics([line_of("the light now", per_word=700)])
    plan = build_plan(lyr, lib, 1.0, track_id="scene", overrides=[songs.Override(line=0, word=1, categories=categories)])
    fx = plan.get(0, 1)
    sc = Scene(lyr, demo_cover(), (270, 480), style=Style(), wordfx=plan)
    try:
        t = fx.match.begin_ms + (fx.match.end_ms - fx.match.begin_ms) * frac
        return sc.snapshot(t).toarray(colorType=skia.kRGBA_8888_ColorType).tobytes()
    finally:
        sc.close()


def test_same_frame_is_pixel_identical_across_scenes(lib):
    """Preview and export build their own Scene: a frame must be a pure function of (plan, time)."""
    assert _scene_frame(lib) == _scene_frame(lib)


def test_streak_reaches_the_real_frame_and_can_be_turned_off(lib, restore_fx):
    with_streak = _scene_frame(lib)
    config.set_fx(config.with_fx(streak_by_category={}))
    assert _scene_frame(lib) != with_streak


def test_every_default_variant_of_every_category_renders_in_a_scene(lib, restore_fx):
    from marea.wordfx import variants as V
    cats = sorted(V.VARIANTS)
    for cat in cats:
        for v in V.options_for(cat):
            config.set_fx(config.with_fx(variant_weights={f"{cat}.{o.name}": float(o.name == v.name) for o in V.options_for(cat)}))
            assert len(_scene_frame(lib, categories=(cat,), frac=0.5)) == 270 * 480 * 4
