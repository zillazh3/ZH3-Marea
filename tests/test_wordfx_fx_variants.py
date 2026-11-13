"""Tests for category FX variants and custom particle systems."""
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import library
from marea.wordfx.anim import particles
from marea.wordfx.matcher import match_line
from marea.wordfx.plan import build_plan


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def _line(text: str, start: int = 1000, dur: int = 1400) -> Line:
    ws = text.split()
    step = dur // len(ws)
    syls = [Syllable(w, start + i * step, start + (i + 1) * step, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


@pytest.mark.parametrize("phrase,word,cat,kind", [
    ("arde el fuego", "fuego", "fire", "ember"),
    ("lloro de tristeza", "tristeza", "sadness", "raindrop"),
    ("quiero llorar", "llorar", "sadness", "tear"),
    ("tears as I cry", "cry", "sadness", "tear"),
    ("bajo el agua", "agua", "water", "bubble"),
    ("mi nube blanca", "nube", "cloud", "mist"),
    ("todo está roto", "roto", "broken", "shard"),
    ("como un sueño", "sueño", "dream", "wisp"),
    ("brilla el aire", "aire", "air", "seed"),
    ("logra cicatrizar", "cicatrizar", "healing", "wisp"),
    ("abierta la herida", "herida", "sadness", "crimson"),
    ("deep open wound", "wound", "anger", "crimson"),
    ("never let me bleed", "bleed", "anger", "crimson"),
])
def test_category_fx_variants_emit_custom_particles(lib, phrase, word, cat, kind):
    line = _line(phrase)
    ms = {m.text: m.categories for m in match_line(line, 0, lib)}
    assert cat in ms[word]

    plan = build_plan(Lyrics([line]), lib, track_id=f"variant_{cat}")
    wi = phrase.split().index(word)
    fx = plan.get(0, wi)
    assert fx is not None
    assert fx.params.particle_kind == kind
    assert fx.params.particles >= 10

    b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
    xs = [i * 0.6 for i in range(len(word))]
    ws = [0.55] * len(word)
    emitted = particles(fx.params, xs, ws, b0 + 0.9, b0, b1)
    assert emitted and any(q.kind == kind for q in emitted)


def test_tears_drip_and_ripple_below_word(lib):
    line = _line("lloro sin parar", start=1000, dur=1800)
    plan = build_plan(Lyrics([line]), lib, track_id="tears_ripple")
    fx = plan.get(0, 0)
    assert fx is not None and fx.params.particle_kind == "tear"
    b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
    xs = [i * 0.6 for i in range(5)]
    ws = [0.55] * 5
    early = particles(fx.params, xs, ws, b0 + 0.55, b0, b1)
    late = particles(fx.params, xs, ws, b0 + 1.25, b0, b1)
    assert any(q.kind == "tear" for q in early)
    assert any(q.kind == "ripple" for q in late)


def test_wound_bleed_drips_crimson_and_stains(lib):
    line = _line("duele la herida", start=1000, dur=1800)
    plan = build_plan(Lyrics([line]), lib, track_id="wound_crimson")
    fx = plan.get(0, 2)
    assert fx is not None
    assert fx.params.particle_kind == "crimson"
    assert fx.params.waveform == "breath" and fx.params.sig_axis == "scale"
    assert fx.params.fracture >= 0.4 and fx.params.chromatic_em >= 0.01
    b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
    xs = [i * 0.6 for i in range(6)]
    ws = [0.55] * 6
    early = particles(fx.params, xs, ws, b0 + 0.65, b0, b1)
    late = particles(fx.params, xs, ws, b0 + 1.55, b0, b1)
    assert any(q.kind == "crimson" for q in early)
    assert any(q.kind == "stain" for q in late)


def test_scar_kintsugi_gold_seam(lib):
    line = _line("queda una cicatriz", start=1000, dur=1800)
    plan = build_plan(Lyrics([line]), lib, track_id="scar_kintsugi")
    fx = plan.get(0, 2)
    assert fx is not None
    assert {"sadness", "healing"} <= set(fx.match.categories)
    assert fx.params.particle_kind == "wisp"
    assert fx.params.fracture >= 0.15 and fx.params.sparkle >= 4


def test_fx_variants_render_cleanly_in_scene(lib):
    lines = [
        _line("fuego y burbujas en el agua", start=500, dur=2000),
        _line("sueño roto en nube de aire", start=3000, dur=2000),
        _line("lloro de tristeza hasta cicatrizar", start=5500, dur=2000),
        _line("solo quiero llorar", start=8000, dur=2000),
        _line("abierta la herida deja cicatriz", start=10500, dur=2200),
    ]
    lyrics = Lyrics(lines)
    plan = build_plan(lyrics, lib, track_id="variants_render")
    scene = Scene(lyrics, demo_cover(), (270, 480), style=Style(), wordfx=plan)
    try:
        for line in lines:
            img = scene.snapshot(line.start + (line.end - line.start) * 0.65)
            arr = img.toarray(colorType=skia.kRGBA_8888_ColorType)
            assert arr.shape == (480, 270, 4)
    finally:
        scene.close()
