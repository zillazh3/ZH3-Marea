"""Tests for the four seasonal categories (spring, summer, autumn, winter) and their custom particles."""
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import config, library
from marea.wordfx.anim import particles
from marea.wordfx.matcher import match_line
from marea.wordfx.plan import build_plan


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


@pytest.fixture(autouse=True)
def every_fx_word_is_strong():
    """These tests check each category's own FX; staging (one strong word per line) is covered in test_wordfx_staging.py."""
    prev = config.set_fx(config.with_fx(max_strong_per_line=9))
    yield
    config.set_fx(prev)


def _line(text: str, start: int = 1000, dur: int = 1200) -> Line:
    ws = text.split()
    step = dur // len(ws)
    syls = [Syllable(w, start + i * step, start + (i + 1) * step, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


@pytest.mark.parametrize("phrase,word,cat,kind", [
    ("en primavera", "primavera", "spring", "petal"),
    ("sweet springtime", "springtime", "spring", "petal"),
    ("tarde de verano", "verano", "summer", "firefly"),
    ("summer fireflies", "summer", "summer", "firefly"),
    ("viento otoñal", "otoñal", "autumn", "leaf"),
    ("autumn foliage", "autumn", "autumn", "leaf"),
    ("frío invierno", "invierno", "winter", "snow"),
    ("snowflakes fall", "snowflakes", "winter", "snow"),
])
def test_seasons_match_compose_and_emit_custom_particles(lib, phrase, word, cat, kind):
    line = _line(phrase)
    ms = {m.text: m.categories for m in match_line(line, 0, lib)}
    assert cat in ms[word]

    plan = build_plan(Lyrics([line]), lib, track_id=f"season_{cat}")
    wi = phrase.split().index(word)
    fx = plan.get(0, wi)
    assert fx is not None
    assert fx.params.particle_kind == kind
    assert fx.params.particles >= 15

    b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
    xs = [i * 0.6 for i in range(len(word))]
    ws = [0.55] * len(word)
    emitted = particles(fx.params, xs, ws, b0 + 0.9, b0, b1)
    assert emitted and any(q.kind == kind for q in emitted)


def test_seasons_render_cleanly_in_scene(lib):
    lines = [
        _line("primavera y cerezos", start=500, dur=2000),
        _line("luciérnagas de verano", start=3000, dur=2000),
        _line("hojarasca del otoño", start=5500, dur=2000),
        _line("nieve en invierno", start=8000, dur=2000),
    ]
    lyrics = Lyrics(lines)
    plan = build_plan(lyrics, lib, track_id="seasons_render")
    scene = Scene(lyrics, demo_cover(), (270, 480), style=Style(), wordfx=plan)
    try:
        for line in lines:
            img = scene.snapshot(line.start + (line.end - line.start) * 0.65)
            arr = img.toarray(colorType=skia.kRGBA_8888_ColorType)
            assert arr.shape == (480, 270, 4)
    finally:
        scene.close()
