"""Lo que el prototipo no puede dar como referencia numérica: vibrato (fases aleatorias) y transiciones CSS."""
import pytest

from marea.demo_song import exact_lyrics
from marea.engine import curves
from marea.engine.engine import Engine, Params, Tween
from marea.engine.layout import blur_level
from marea.model.lyrics import parse_ttml


def measure(line, active):
    return 60.0 + (30.0 if active and line.bg else 0.0)


def letters_at(t_ms, **params):
    eng = Engine(exact_lyrics(), measure, 900, params=Params(**params))
    eng.warmup(t_ms)
    return [c for L in eng.items for w in L.words if w.emph for c in w.parts], eng


def test_vibrato_amplitude_bounds_and_toggle():
    # "mar~" (letras) suena entre 11.89 s y 13.0 s
    on, _ = letters_at(12400, vib=True)
    off, _ = letters_at(12400, vib=False)
    assert any(a.x != 0 for a in on) and all(b.x == 0 for b in off)
    assert all(abs(a.x) <= 0.006 + 1e-9 for a in on)
    assert all(abs(a.y - b.y) <= 0.011 + 1e-9 for a, b in zip(on, off))


def test_emph_rules():
    _, eng = letters_at(0)
    emph = {w.text for L in eng.items for w in L.words if w.emph}
    # ≥ 1 s y 2–7 letras. "voz" dura 0.954 s -> no; "ciudad" y "Camino" son largas pero rápidas -> no
    # ("marea" es la voz de fondo ma|re|a~ de 1.4 s: el prototipo también aplica énfasis ahí)
    assert emph == {"mar", "azul", "luz", "marea", "apaga", "aquí", "deriva"}


def test_tween_css_semantics():
    tw = Tween(0.0, 400, curves.EASE)
    tw.to(1.0, 1000.0)
    assert tw.value(1000.0) == 0.0 and tw.value(1400.0) == 1.0 and tw.value(2000.0) == 1.0
    mid = tw.value(1200.0)
    assert mid == pytest.approx(curves.EASE(0.5)) and 0.5 < mid < 1  # ease acelera al inicio
    tw.to(0.0, 1200.0)  # reversa a medio camino: parte del valor actual, no de 1.0
    assert tw.value(1200.0) == pytest.approx(mid) and tw.value(1600.0) == 0.0


def test_bg_vocal_slides_in_and_line_transitions():
    eng = Engine(exact_lyrics(), measure, 900)
    eng.warmup(14000)  # línea 3 (13.4 s) activa, con voz de fondo
    a = eng.items[eng.active]
    assert a.on and a.words[-1].bg
    assert a.act == pytest.approx(1.0) and a.bg_p == pytest.approx(1.0) and a.bg_op == pytest.approx(1.0)
    assert a.opacity == pytest.approx(0.85)
    prev = eng.items[eng.active - 1]
    assert prev.act == 0 and prev.bg_op == 0 and prev.blur == pytest.approx(3.0)  # arriba: dist = activa-i+1 = 2
    # justo tras el cambio de línea, el blur y el brillo aún están a medio camino
    eng.warmup(13200)
    before = eng.active
    while eng.active == before:
        eng.advance(1 / 60)
    eng.advance(1 / 60)
    eng.advance(1 / 60)
    new, old = eng.items[eng.active], eng.items[before]
    assert 0 < new.act < 1 and 0 < old.act < 1 and 0 < new.opacity < 1


TTML_OVERLAP = """<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body><div>
<p begin="0:01.000" end="0:03.000"><span begin="0:01.000" end="0:03.000">uno</span><span ttm:role="x-bg"><span begin="0:03.500" end="0:04.000">(ey</span><span begin="0:04.000" end="0:05.500">ey)</span></span></p>
<p begin="0:04.200" end="0:06.000"><span begin="0:04.200" end="0:06.000">dos</span></p>
</div></body></tt>"""


def test_bg_vocal_keeps_singing_after_next_line_starts():
    eng = Engine(parse_ttml(TTML_OVERLAP), measure, 900)
    eng.warmup(4800)  # línea 2 ya activa; el x-bg de la 1 canta hasta 5.5 s
    prev = eng.items[0]
    bg = [w for w in prev.words if w.bg][-1]
    assert eng.active == 1 and prev.on and 0 in eng.live
    assert prev.bg_op == pytest.approx(1.0) and prev.act == pytest.approx(1.0) and prev.blur_tw.t == 0
    assert 0 < bg.p < 1 and any(c.glow_a > 0 for c in bg.parts)
    eng.warmup(6500)  # el x-bg terminó: la línea 1 ya es «pasada»
    assert not prev.on and prev.bg_op == pytest.approx(0.0) and prev.blur_tw.t > 0


def test_blur_levels():
    # arriba: min(5, 2 + activa - i); abajo: min(5, 1 + i - activa)
    assert [blur_level(i, 5) for i in range(0, 10)] == [5, 5, 5, 4, 3, 0, 2, 3, 4, 5]
    assert blur_level(2, 5, blurm=0) == 0 and blur_level(6, 5, scale=0.8) == pytest.approx(1.6)
