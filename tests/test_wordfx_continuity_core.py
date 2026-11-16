"""Checkpoint 1 tests: deterministic springs, 6-phase envelope, duration modes, seed uniqueness, and base parity."""
import numpy as np
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.engine.tuning import TUNING, Tuning
from marea.render import scene as scene_mod
from marea.render.scene import Scene, Style
from marea.wordfx import config, library
from marea.wordfx.composer import seed_for
from marea.wordfx.envelope import duration_mode, sample_envelope
from marea.wordfx.plan import build_plan
from marea.wordfx.springs import SIM_DT, SpringChannel, eval_spring_240hz, step_spring


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def test_critically_damped_vs_underdamped_spring():
    # Critically damped (zeta=1.0) approaches target monotonically without overshoot
    p, v = 0.0, 0.0
    vals_crit = []
    for _ in range(120):
        p, v = step_spring(p, v, 1.0, 2.5, 1.0, SIM_DT)
        vals_crit.append(p)
    assert max(vals_crit) <= 1.0 + 1e-6
    assert vals_crit[-1] == pytest.approx(1.0, abs=5e-3)

    # Underdamped (zeta=0.8) overshoots slightly (> 1.0) before settling
    p, v = 0.0, 0.0
    vals_under = []
    for _ in range(120):
        p, v = step_spring(p, v, 1.0, 2.5, 0.8, SIM_DT)
        vals_under.append(p)
    assert max(vals_under) > 1.01
    assert vals_under[-1] == pytest.approx(1.0, abs=1e-2)


def test_retarget_preserves_velocity():
    ch = SpringChannel(p=0.0, v=0.0, g=1.0, freq_hz=2.5, zeta=1.0)
    for _ in range(20):
        ch.step(SIM_DT)
    v_before = ch.v
    p_before = ch.p
    assert v_before > 0.0
    ch.retarget(0.2)
    assert ch.p == p_before and ch.v == v_before
    ch.step(SIM_DT)
    # Positive velocity continues carrying p slightly upward briefly before curving toward 0.2
    assert ch.p > p_before


def test_240hz_determinism_seek_vs_linear_play():
    def target_fn(t):
        return 1.0 if 1.0 <= t <= 1.6 else 0.2 if t > 1.6 else 0.0

    t_origin = 0.8
    # Direct seek to various t values out of order
    samples_seek = [eval_spring_240hz(target_fn, t, t_origin) for t in (2.1, 1.25, 1.65, 1.05)]
    # Forward order at different frame rates (60 fps vs 30 fps)
    samples_60 = [eval_spring_240hz(target_fn, t, t_origin) for t in (1.05, 1.25, 1.65, 2.1)]
    assert samples_seek == [samples_60[3], samples_60[1], samples_60[2], samples_60[0]]


def test_duration_modes_and_log_cap():
    assert duration_mode(0.18) == ("accent", 0.40)
    assert duration_mode(0.45) == ("normal", 1.0)
    m_long, p_long = duration_mode(1.4)
    m_vlong, p_vlong = duration_mode(10.0)
    assert m_long == "held" and 1.0 < p_long < p_vlong <= 1.85


def test_envelope_six_phases():
    prev = config.set_fx(config.with_fx(residue_gain=1.0, past_line_tint=0.05))  # the category residue, to see every phase
    try:
        _six_phases()
    finally:
        config.set_fx(prev)


def test_default_envelope_returns_to_the_plain_look_after_the_release():
    kw = dict(is_hero=True, release_s=0.6, residue_level=0.18, line_end_s=4.0, fade_s=0.4)
    after = sample_envelope(2.6 + 0.6 + 0.8, 2.0, 2.6, **kw)  # well after the release, the line still active
    assert after.motion_amp < 0.01 and after.tint_amp < 0.01


def _six_phases():
    b0, b1 = 2.0, 2.6  # normal 600 ms word
    kw = dict(is_hero=True, release_s=0.6, residue_level=0.18, line_end_s=4.0, fade_s=0.4)
    idle = sample_envelope(1.5, b0, b1, **kw)
    anti = sample_envelope(b0 - TUNING.anticipation_s * 0.6, b0, b1, **kw)
    atk = sample_envelope(2.05, b0, b1, **kw)
    sus = sample_envelope(2.50, b0, b1, **kw)
    rel = sample_envelope(2.90, b0, b1, **kw)
    res = sample_envelope(3.60, b0, b1, **kw)
    fade = sample_envelope(5.50, b0, b1, **kw)

    assert idle.phase == "idle" and idle.motion_amp == 0.0
    assert anti.phase == "anticipation" and 0.0 < anti.motion_amp <= config.FX.anticipation_amp + 0.02
    assert atk.phase == "attack" and atk.motion_amp > anti.motion_amp
    assert sus.phase == "sustain" and sus.motion_amp > 0.7
    assert rel.phase == "release" and 0.18 < rel.motion_amp < sus.motion_amp
    assert res.phase == "residue" and res.motion_amp == pytest.approx(0.18, abs=0.02)
    assert fade.phase == "fade" and fade.motion_amp < 0.01 and fade.tint_amp == pytest.approx(config.FX.past_line_tint)


def test_non_hero_has_no_anticipation():
    s = sample_envelope(2.0 - TUNING.anticipation_s * 0.6, 2.0, 2.6, is_hero=False)
    assert s.phase == "idle" and s.motion_amp == 0.0


def test_seed_for_depends_on_line_text_and_word_not_on_repetition():
    assert seed_for("song", "a b c", 2) == seed_for("song", "a b c", 2)
    assert seed_for("song", "a b c", 2) != seed_for("song", "a b d", 2)
    assert seed_for("song", "a b c", 2) != seed_for("song", "a b c", 3)


def test_base_parity_at_zero_intensity(lib):
    lyrics = exact_lyrics()
    cover = demo_cover()
    plan_zero = build_plan(lyrics, lib, intensity=0.0, track_id="parity")
    s_base = Scene(lyrics, cover, (270, 480), style=Style(), wordfx=None)
    s_zero = Scene(lyrics, cover, (270, 480), style=Style(), wordfx=plan_zero)
    for t_ms in (7000, 10500, 14500, 22500):
        a = s_base.snapshot(t_ms).toarray(colorType=skia.kRGBA_8888_ColorType)
        b = s_zero.snapshot(t_ms).toarray(colorType=skia.kRGBA_8888_ColorType)
        assert np.array_equal(a, b), f"Parity mismatch at {t_ms} ms"


def test_idle_fx_word_looks_the_same_on_the_cheap_and_the_full_path(lib, monkeypatch):
    """Before its onset an FX word is drawn by the cheap base path; switching to the full FX path must not move a single letter."""
    lyrics, cover = exact_lyrics(), demo_cover()
    plan = build_plan(lyrics, lib, track_id="idle-switch")
    calls = []

    def frames(anticipation):
        monkeypatch.setattr(scene_mod, "TUNING", Tuning(anticipation_s=anticipation))
        out = []
        for t_ms in (7000, 10500, 14500, 22500):
            scene = Scene(lyrics, cover, (270, 480), style=Style(), wordfx=plan)
            orig = scene._fx_word
            scene._fx_word = lambda *a, **k: calls.append(1) or orig(*a, **k)
            out.append(scene.snapshot(t_ms).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int))
        return out, len(calls)

    cheap, cheap_calls = frames(TUNING.anticipation_s)
    calls.clear()
    full, full_calls = frames(10.0)  # a huge anticipation switches the cheap path off: every FX word takes the full path
    assert full_calls > cheap_calls, "the cheap path was never exercised"
    for a, b in zip(cheap, full):
        assert np.abs(a - b).max() <= 3
