"""Glow/bloom: soft, follows the fill, fades with the line, derives its colour from the cover, never clipped into a box."""
import numpy as np
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.engine.engine import Engine, Params
from marea.engine.tuning import TUNING
from marea.render import scene as scene_mod
from marea.render.scene import BLOOM_STEPS, Scene, Style
from marea.wordfx.render import _GLOW_SIGMAS

T_MID_LINE = 10300  # "las luces" is sung, "se derriten en" is not yet (see the rendered frame)
SUNG, UNSUNG = (slice(340, 400), slice(45, 195)), (slice(340, 400), slice(215, 405))  # (rows, cols) at 540x960


def frame(t_ms=T_MID_LINE, bloom=True, wordfx=None, act=None):
    sc = Scene(exact_lyrics(), demo_cover(), (540, 960), style=Style(bloom=bloom), wordfx=wordfx)
    sc.engine.warmup(t_ms)
    if act is not None:
        for L in sc.engine.items:
            if L.on:
                L.act = act
    s = skia.Surface(540, 960)
    sc.render(s.getCanvas())
    return sc, s.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)[..., :3].astype(int)


def bloom_delta(**kw):
    """Mean absolute pixel difference caused by the bloom, over (sung, unsung) text regions."""
    _, on = frame(bloom=True, **kw)
    _, off = frame(bloom=False, **kw)
    d = np.abs(on - off).sum(axis=2)
    return d[SUNG].mean(), d[UNSUNG].mean()


def test_bloom_only_surrounds_sung_text():
    sung, unsung = bloom_delta()
    assert sung > 1.0, "the sung text should get a halo"
    assert unsung < 0.15 * sung, "unsung text must not glow"


def test_bloom_fades_with_the_line_instead_of_popping():
    levels = [bloom_delta(act=a)[0] for a in (0.25, 0.5, 1.0)]
    assert 0 < levels[0] < levels[1] < levels[2]
    assert scene_mod.Scene._bloom(frame()[0], 60.0, 0.0) is None  # no halo at all once the line is inactive


def test_bloom_gain_is_a_real_knob(monkeypatch):
    base = bloom_delta()[0]
    monkeypatch.setattr(scene_mod, "TUNING", type(TUNING)(bloom_gain=TUNING.bloom_gain * 2))
    assert bloom_delta()[0] > base * 1.4
    monkeypatch.setattr(scene_mod, "TUNING", type(TUNING)(bloom_gain=0.0))
    assert bloom_delta() == (0.0, 0.0)


def test_halo_is_tinted_by_the_cover_not_pure_white(monkeypatch):
    sc = frame()[0]
    r, g, b = sc._glow_rgb()
    assert (r, g, b) != (255, 255, 255) and min(r, g, b) >= 0 and max(r, g, b) <= 255
    assert b >= r  # demo cover is blue: the halo leans blue, it does not fight the background
    sc2 = Scene(exact_lyrics(), demo_cover(), (270, 480), style=Style())
    monkeypatch.setattr(scene_mod, "TUNING", type(TUNING)(glow_white_mix=1.0))
    assert sc2._glow_rgb() == (255, 255, 255)


def test_glow_layers_are_large_enough_not_to_clip_the_widest_halo():
    widest = 3 * max(_GLOW_SIGMAS)  # em: a halo is visible out to ~3 sigma
    max_offset = 0.08  # em: largest semantic displacement (wordfx.anim max_off, held hero word)
    assert TUNING.glow_pad_em >= widest + max_offset


def test_emphasised_letters_do_not_glow_ahead_of_the_wipe():
    eng = Engine(exact_lyrics(), lambda line, active: 60.0, 900, params=Params(vib=False))
    emph = [w for L in eng.items if L.kind == "line" for w in L.words if w.emph]
    assert emph
    word = emph[0]
    eng.warmup(word.b * 1000 - 300)
    seen = 0
    for _ in range(int((word.e - word.b + 1.5) * 60)):
        eng.advance(1 / 60)
        for c in word.parts:
            if c.glow_a > 0:
                seen += 1
                assert c.p > 0.0, "a letter is glowing before the wipe reached it"
    assert seen > 0


@pytest.mark.parametrize("t_ms", [7000, 12200, 14500])
def test_bloom_frames_are_deterministic(t_ms):
    assert np.array_equal(frame(t_ms)[1], frame(t_ms)[1])
    assert BLOOM_STEPS >= 8
