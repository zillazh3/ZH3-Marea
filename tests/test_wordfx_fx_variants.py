"""Tests for category FX variants and custom particle systems."""
from dataclasses import replace

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


def test_fire_ember_cooling_sparkle_style_and_glow_shimmer(lib):
    line = _line("arde el fuego", start=1000, dur=2000)
    plan = build_plan(Lyrics([line]), lib, track_id="fire_ember_grade")
    fx = plan.get(0, 2)
    assert fx is not None
    assert fx.params.particle_kind == "ember"
    assert fx.params.sparkle_style == "ember"
    b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
    xs = [i * 0.6 for i in range(5)]
    ws = [0.55] * 5
    pts = [q for dt in (0.35, 0.75, 1.25, 1.65) for q in particles(fx.params, xs, ws, b0 + dt, b0, b1)]
    embers = [q for q in pts if q.kind == "ember"]
    sparks = [q for q in pts if q.kind == "spark"]
    assert embers and all(q.core_color is not None and 0.0 <= q.life_u <= 1.0 for q in embers)
    young = [q for q in embers if q.life_u < 0.25]
    old = [q for q in embers if q.life_u > 0.65]
    if young and old:
        avg_b_young = sum(q.color[2] for q in young) / len(young)
        avg_b_old = sum(q.color[2] for q in old) / len(old)
        assert avg_b_young >= avg_b_old  # young embers hold hotter blue/white channel than cooling ash-red embers
    assert sparks and all(q.style == "ember" for q in sparks)


def test_sparkle_styles_differentiated_across_categories(lib):
    samples = [
        ("celestial", _line("bajo la estrella fugaz", start=1000, dur=2000), 2),
        ("buoyant", _line("rebosa alegria hoy", start=1000, dur=2000), 1),
        ("bokeh", _line("siento amor profundo", start=1000, dur=2000), 1),
        ("flare", _line("queda la foto guardada", start=1000, dur=2000), 2),
    ]
    for expected_style, line, wi in samples:
        plan = build_plan(Lyrics([line]), lib, track_id=f"style_{expected_style}")
        fx = plan.get(0, wi)
        assert fx is not None, expected_style
        assert fx.params.sparkle_style == expected_style
        b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
        p_use = fx.params if fx.params.sparkle > 0 else replace(fx.params, sparkle=6)
        sparks = [q for q in particles(p_use, [0.0, 0.6, 1.2], [0.55] * 3, (b0 + b1) * 0.5, b0, b1) if q.kind == "spark"]
        assert sparks and all(q.style == expected_style for q in sparks)


def test_wisp_velocity_hints_present(lib):
    line = _line("como un sueño", start=1000, dur=2000)
    plan = build_plan(Lyrics([line]), lib, track_id="wisp_velocity")
    fx = plan.get(0, 2)
    assert fx is not None and fx.params.particle_kind == "wisp"
    b0, b1 = fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0
    xs = [i * 0.6 for i in range(5)]
    wisps = [q for dt in (0.3, 0.6, 0.9) for q in particles(fx.params, xs, [0.55] * 5, b0 + dt, b0, b1) if q.kind == "wisp"]
    assert wisps and all(abs(q.vx_em) + abs(q.vy_em) > 0.0 for q in wisps) and sum(q.vy_em for q in wisps) < 0.0
