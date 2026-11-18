"""Continuity of the actual carousel continuation, including virtual tick L."""
import numpy as np
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.model.background import BackgroundConfig
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.model.loop import LoopSpec
from marea.render.scene import Scene, Style


def fixture(gap=False):
    lines = [Line(1000, 1800, syllables=[Syllable("First", 1000, 1800)]),
             Line(2500, 3300, agent="v2", syllables=[Syllable("Second", 2500, 3300)])]
    ticks = 360 if gap else 210
    spec = LoopSpec(start_ms=500, first=0, last=1, ticks=ticks, video_fade_ms=250)
    lyrics = Lyrics(lines, credit="Provider", writers=["Writer"])
    scene = Scene(lyrics, demo_cover(), (180, 320), loop=spec,
                  background=BackgroundConfig(source="transparent"),
                  style=Style(wm_text="Watermark"))
    return scene, spec


def pixels(scene, tick):
    scene.seek_loop(tick)
    surface = skia.Surface(scene.w, scene.h)
    scene.render(surface.getCanvas())
    return surface.makeImageSnapshot().toarray().astype(float)


@pytest.mark.parametrize("gap", [False, True])
def test_virtual_endpoint_is_live_continuation_with_matching_position_velocity(gap):
    scene, spec = fixture(gap)
    first = pixels(scene, 0)
    initial = [(item.y.p, item.y.v, item.s.p) for item in scene.engine.items
               if item.loop_cycle == 0]
    near_start = pixels(scene, 1)
    near_end = pixels(scene, spec.ticks - 1)
    last = pixels(scene, spec.ticks)
    final = [(item.y.p, item.y.v, item.s.p) for item in scene.engine.items
             if item.loop_cycle == 1]
    assert scene.engine.t == pytest.approx(spec.end_ms / 1000)
    assert np.allclose(initial, final, atol=0.01)
    assert np.abs(first - last).mean() < 0.03
    assert np.abs((near_start - first) - (last - near_end)).mean() < 1.0
    assert not any(item.is_credit for item in scene.engine.items)


def test_random_seek_matches_sequential_and_keeps_original_word_times():
    scene, spec = fixture()
    sequential = pixels(scene, 95)
    pixels(scene, spec.ticks)
    assert np.array_equal(pixels(scene, 95), sequential)
    words = [item.words[0] for item in scene.engine.items if not item.is_gap]
    assert set(word.b for word in words) == {1.0, 2.5}
    assert all(word.word_index == 0 for word in words)
    with pytest.raises(ValueError):
        scene.seek_loop(spec.ticks + 1)


def test_cover_and_adaptation_probe_use_the_same_loop_phase():
    scene, spec = fixture()
    scene.set_background(BackgroundConfig())
    first = pixels(scene, 0)
    probe = scene._backdrop(20, 70, 100, 80)
    last = pixels(scene, spec.ticks)
    assert np.abs(first - last).mean() < 0.03
    assert scene._backdrop(20, 70, 100, 80) == probe


def test_wordfx_clones_keep_source_matches_and_seam_pixels():
    from marea.wordfx.fxtypes import FxParams, FxWord
    from marea.wordfx.matcher import Match
    from marea.wordfx.plan import FxPlan

    scene, spec = fixture()
    match = Match(0, 0, "First", "first", "en", (), 1000, 1800)
    params = FxParams(seed=123, vector=(0.0,) * 6, primitives=(),
                      color_from=(255, 180, 80), color_to=(90, 180, 255),
                      wave_em=0.05, wave_hz=1.0, trail=0.8, particles=8)
    wordfx = FxWord(match, params)
    scene.attach_wordfx(scene.engine.lyrics, FxPlan({(0, 0): wordfx}))
    clones = [item.words[0] for item in scene.engine.items if item.src is scene.engine.lyrics.lines[0]]
    assert all(word.fx is wordfx for word in clones)
    assert np.abs(pixels(scene, 0) - pixels(scene, spec.ticks)).mean() < 0.03


def test_single_line_loop_has_neighbors_and_preserves_layout_at_seam():
    lyrics = Lyrics([Line(100400, 101000, syllables=[Syllable("Only", 100400, 101000)])])
    spec = LoopSpec(100000, 0, 0, 77)
    scene = Scene(lyrics, demo_cover(), (180, 320), loop=spec,
                  background=BackgroundConfig(source="transparent"))
    first = pixels(scene, 0)
    states = {(item.loop_cycle, item.kind): (item.y.p, item.y.v, item.s.p)
              for item in scene.engine.items
              if item.y.p + item.h > -30 and item.y.p < scene.stage.height() + 30}
    after_start = pixels(scene, 1)
    before_end = pixels(scene, spec.ticks - 1)
    end = pixels(scene, spec.ticks)
    continued = {(item.loop_cycle - 1, item.kind): (item.y.p, item.y.v, item.s.p)
                 for item in scene.engine.items}
    for key, state in states.items():
        assert np.allclose(state, continued[key], atol=0.01), key
    assert np.abs(first - end).mean() < 0.03
    # Compare both neighboring frames to a real subsequent cycle, allowing the
    # spring's acceleration rather than treating a discrete second derivative as a jump.
    scene.engine.seek_tick(spec.ticks + 1)
    scene._loop_phase_ms = (spec.ticks + 1) * 1000 / 60
    surface = skia.Surface(scene.w, scene.h)
    scene.render(surface.getCanvas())
    after_end = surface.makeImageSnapshot().toarray().astype(float)
    assert np.abs((after_start - first) - (after_end - end)).mean() < 0.03
    scene.engine.seek_tick(2 * spec.ticks - 1)
    scene._loop_phase_ms = (2 * spec.ticks - 1) * 1000 / 60
    scene.render(surface.getCanvas())
    next_before_end = surface.makeImageSnapshot().toarray().astype(float)
    assert np.abs(before_end - next_before_end).mean() < 0.03
    assert sum(-50 < item.y.p < scene.stage.height() for item in scene.engine.items) >= 2
