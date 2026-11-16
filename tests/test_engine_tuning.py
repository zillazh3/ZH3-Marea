"""The look-and-feel knobs in engine/tuning.py actually drive the engine, and the defaults keep the dynamics the user asked for."""
import dataclasses

import pytest

from marea.demo_song import exact_lyrics
from marea.engine import curves, engine as engine_mod, layout
from marea.engine.engine import Engine, Params, _lift
from marea.engine.tuning import TUNING, Tuning

DT = 1 / 60


def measure(line, active):
    return 60.0 + (30.0 if active and line.bg else 0.0)


def engine_at(t_ms):
    eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=False))
    eng.warmup(t_ms)
    return eng


def test_tuning_is_frozen_and_documented():
    with pytest.raises(dataclasses.FrozenInstanceError):
        TUNING.stagger_ms = 0.0
    src = open(engine_mod.__file__.replace("engine.py", "tuning.py"), encoding="utf-8").read()
    assert src.count("#") > len(dataclasses.fields(Tuning))


def test_lines_below_the_active_one_follow_in_a_cascade():
    eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=False))
    eng.warmup(11000)
    eng.advance(DT)
    t0 = eng.now
    for _ in range(300):  # advance until the next active-line change, then look at the pending stagger delays
        before = eng.active
        eng.advance(DT)
        if eng.active != before:
            break
    below = [L.y.pending[1] for L in eng.items[eng.active:] if L.y.pending is not None]
    assert len(below) >= 3 and below == sorted(below) and below[-1] - below[0] >= 2 * TUNING.stagger_ms * 0.5
    assert t0 < below[0]


def test_inactive_lines_blur_more_with_distance_and_are_dimmer():
    eng = engine_at(12000)
    a = eng.active
    blurs = [eng.items[i].blur_tw.t for i in range(a + 1, a + 5)]
    assert blurs == sorted(blurs) and blurs[0] > 0 and blurs[-1] > blurs[0]
    assert eng.items[a].blur_tw.t == 0.0 and eng.items[a].op_tw.t == TUNING.active_opacity
    assert eng.items[a + 1].op_tw.t == 1.0  # inactive lines are dimmed by alpha (dim_alpha), not by layer opacity


def test_active_line_is_larger_than_inactive_ones():
    eng = engine_at(12000)
    a = eng.active
    assert eng.items[a].s.p == pytest.approx(1.0, abs=1e-3)
    assert eng.items[a + 1].s.p == pytest.approx(TUNING.inactive_scale, abs=1e-3)


def test_blur_and_scroll_follow_the_knobs(monkeypatch):
    monkeypatch.setattr(layout, "TUNING", dataclasses.replace(TUNING, blur_per_line=0.0))
    assert layout.blur_level(5, 2, 1.0) == 0.0
    monkeypatch.setattr(layout, "TUNING", dataclasses.replace(TUNING, scroll_k_base=300.0, scroll_k_range=0.0, scroll_damping=2.0))
    k, c = layout.pos_y_policy(False, False, 400)
    assert k == 300.0 and c == pytest.approx(2.0 * 300.0 ** 0.5)


def test_glow_builds_through_the_syllable_instead_of_dying_early():
    g = curves.glow_curve(TUNING.glow_peak_at, TUNING.glow_end_level)
    xs = [i / 100 for i in range(101)]
    ys = [g(x) for x in xs]
    assert g(0) == 0 and max(ys) <= 1.0
    peak_x = xs[ys.index(max(ys))]
    assert peak_x >= 0.6, "the glow should peak late in the syllable"
    assert ys[: int(peak_x * 100) + 1] == sorted(ys[: int(peak_x * 100) + 1])  # monotonic build
    assert g(1.0) == pytest.approx(TUNING.glow_end_level, abs=1e-6)
    assert curves.GLOW(0.9) < 0.5 < g(0.9)  # the prototype curve is already fading there


def test_lift_scales_only_the_upward_part():
    assert _lift(-0.02, 1.5) == pytest.approx(-0.03)
    assert _lift(0.01, 1.5) == 0.01  # the resting offset (before the syllable is sung) is untouched
    assert _lift(-0.02, 1.0) == -0.02


def test_default_glow_is_moderately_stronger_than_the_prototype():
    assert 1.2 <= TUNING.glow_alpha / 0.35 <= 1.35
    assert 1.2 <= TUNING.emph_glow_alpha / (1.85 * 0.55) <= 1.35


def test_sung_syllable_glow_follows_the_fill_peak_late():
    eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=False))
    L = next(L for L in eng.items if L.kind == "line")
    u = max((u for w in L.words for u in w.parts), key=lambda u: u.e - u.b)
    samples = []
    eng.warmup(max(0, u.b * 1000 - 200))
    for _ in range(int(((u.e - u.b) + 1.0) / DT)):
        eng.advance(DT)
        samples.append((eng.t, u.glow_a))
    mid = max(a for t, a in samples if t < (u.b + u.e) / 2)
    late = max(a for t, a in samples if (u.b + u.e) / 2 <= t <= u.e + 0.05)
    assert late > mid * 0.9  # does not fade away before the note ends
