import math
from dataclasses import replace

import pytest
import skia

from marea.render.text import Fonts
from marea.wordfx.anim import echoes, hash01, letter_state, noise1, particles
from marea.wordfx.fxtypes import FxParams, FxWord
from marea.wordfx.matcher import Match
from marea.wordfx.render import Letter, draw_word_fx, letters_for

B, E = 1.0, 2.0  # intervalo cantado (s)


def P(**kw) -> FxParams:
    return FxParams(seed=12345, vector=(0.0,) * 6, primitives=(), color_from=(255, 200, 100), color_to=(100, 200, 255), **kw)


PERDIDO = dict(drift_em=0.4, erode=0.5, particles=12)
TIEMPO = dict(stagger_ms=80, letter_offset_em=0.3, trail=1.0, wave_em=0.05, wave_hz=1.0)
RESPIRAR = dict(jitter_em=0.03, jitter_hz=6.0, chromatic_em=0.03, spacing_em=-0.05, scale_amp=0.1)
LOVE = dict(glow=1.0, letter_offset_em=0.2, audio_gain=0.5)
EXTREME = dict(drift_em=-3, jitter_em=2, jitter_hz=500, wave_em=2, wave_hz=50, spacing_em=-2, scale_amp=3, scale_period_s=0.01,
               rotation_deg=360, stagger_ms=2000, letter_offset_em=5, opacity_min=0.0, blur_em=2, glow=1, osc_hz=40, osc_jitter_em=2, osc_color=(0, 0, 0), grain=1,
               chromatic_em=1, trail=1, erode=1, particles=500, fracture=1, sparkle=50, audio_gain=1)


# ---------- anim.py (puro) ----------

def test_hash_noise_ranges_and_continuity():
    vals = [hash01(7, i, 3) for i in range(500)]
    assert all(0.0 <= v < 1.0 for v in vals) and len(set(vals)) > 490
    assert hash01(1, 2) == hash01(1, 2) != hash01(1, 3)
    xs = [i * 0.01 for i in range(-300, 300)]
    ns = [noise1(9, x) for x in xs]
    assert all(-1.0 <= v <= 1.0 for v in ns)
    assert max(abs(a - b) for a, b in zip(ns, ns[1:])) < 0.1  # continuo (pendiente acotada)


def test_pure_and_out_of_order():
    p = P(**PERDIDO, **RESPIRAR, **TIEMPO)
    ts = [0.3, 1.5, 0.9, 5.0, -2.0, 1.5]
    a = [letter_state(p, 2, 5, t, B, E, 0.4) for t in ts]
    b = [letter_state(p, 2, 5, t, B, E, 0.4) for t in reversed(ts)][::-1]
    assert a == b and a[1] == a[5]
    assert particles(p, [0, .5, 1], [.5] * 3, 1.9, B, E) == particles(p, [0, .5, 1], [.5] * 3, 1.9, B, E)


@pytest.mark.parametrize("t", [-5.0, 0.0, B, 1.5, E, 3.7, 1e6])
@pytest.mark.parametrize("n", [1, 2, 7])
def test_finite_everywhere(t, n):
    p = P(**EXTREME)
    for i in range(n):
        s = letter_state(p, i, n, t, B, E, 1.0)
        vals = [s.dx, s.dy, s.scale, s.rot_deg, s.alpha, s.blur_em, s.glow]
        assert all(math.isfinite(v) for v in vals) and 0.0 <= s.alpha <= 1.0
    for q in particles(p, [i * 0.5 for i in range(n)], [0.5] * n, t, B, E):
        assert all(math.isfinite(v) for v in (q.x_em, q.y_em, q.r_em, q.alpha))


def test_zero_length_word_is_finite():
    s = letter_state(P(erode=1.0, drift_em=1.0), 0, 1, 1.0, 1.0, 1.0)
    assert math.isfinite(s.alpha) and math.isfinite(s.dy)


def test_erode_only_last_letters_and_stays():
    p = P(erode=0.4)
    n = 5
    after = [letter_state(p, i, n, E + 3, B, E).alpha for i in range(n)]
    assert after[:3] == [1.0] * 3 and after[3] < 0.01 and after[4] < 0.01
    mid = letter_state(p, 4, n, E - 0.1, B, E).alpha
    assert 0.0 < mid < 1.0
    assert letter_state(p, 4, n, B, B, E).alpha == 1.0
    assert letter_state(P(erode=0.06), 0, 1, E + 1, B, E).alpha < 0.01  # mínimo una letra
    assert letter_state(P(erode=0.02), 0, 1, E + 1, B, E).alpha == 1.0


def test_drift_eases_and_holds():
    p = P(drift_em=0.5)
    dy = [letter_state(p, 0, 3, t, B, E).dy for t in (0.0, B, 1.5, E + 0.15, E + 10)]
    assert dy[0] == dy[1] == 0.0 and 0.0 < dy[2] < 0.5 and dy[3] == pytest.approx(0.5) and dy[4] == pytest.approx(0.5)


def test_blur_clears_at_end():
    p = P(blur_em=0.2)
    assert letter_state(p, 0, 3, B, B, E).blur_em == pytest.approx(0.2)
    assert 0 < letter_state(p, 0, 3, 1.5, B, E).blur_em < 0.2
    assert letter_state(p, 0, 3, E, B, E).blur_em == 0.0


def test_stagger_delays_later_letters():
    p = P(stagger_ms=100, letter_offset_em=0.3)
    t = B + 0.15
    a = [letter_state(p, i, 4, t, B, E).alpha for i in range(4)]
    assert a[0] > a[1] > a[2] >= a[3] and a[3] == 0.0
    assert letter_state(p, 0, 4, B - 1, B, E).alpha == 0.0
    assert letter_state(p, 3, 4, E, B, E).alpha == 1.0
    assert letter_state(P(), 3, 4, B - 1, B, E).alpha == 1.0  # sin entrada: siempre visible


def test_reduce_removes_motion_keeps_color_and_blur():
    p = P(**EXTREME)
    p = replace(p, erode=0.0, opacity_min=1.0, stagger_ms=0, letter_offset_em=0.0)
    a, b = (letter_state(p, 1, 4, t, B, E, rms, True) for t, rms in ((1.2, 0.1), (1.9, 0.9)))
    assert (a.dx, a.dy, a.scale, a.rot_deg) == (b.dx, b.dy, b.scale, b.rot_deg) == (0.0, 0.0, 1.0, 0.0)
    assert a.color == b.color and letter_state(p, 1, 4, B, B, E, 0.5, True).blur_em == pytest.approx(2)
    assert particles(p, [0, 1], [1, 1], 1.9, B, E, reduce=True) == []
    assert letter_state(P(erode=1.0), 0, 1, E + 1, B, E, 0.0, True).alpha < 0.01  # erode sigue en reduce


def test_particles_lifecycle():
    assert particles(P(erode=0.5), [0, .5], [.5, .5], 1.9, B, E) == []
    p = P(**PERDIDO)
    xs, ws = [0, .5, 1, 1.5], [.5] * 4
    assert particles(p, xs, ws, B, B, E) == []
    seen = [q for t in (1.7, 1.9, 2.1, 2.5) for q in particles(p, xs, ws, t, B, E)]
    assert seen and all(q.kind == "dust" and 0.035 <= q.r_em <= 0.09 and 0 <= q.alpha <= 1 for q in seen)
    assert all(q.x_em >= 1.0 for q in seen)  # solo de las letras erosionadas (2 últimas)
    assert particles(p, xs, ws, E + 1.6, B, E) == []
    s = particles(P(sparkle=6), xs, ws, 1.5, B, E)
    assert len(s) == 6 and all(q.kind == "spark" for q in s)
    assert particles(P(sparkle=6), xs, ws, E + 3.2, B, E) == [] == particles(P(sparkle=6), xs, ws, B - 0.5, B, E)


def test_osc_color_and_jitter():
    p = P(osc_hz=2.0, osc_color=(0, 0, 0), osc_jitter_em=0.1, osc_phase=0.0)
    # w = 0.5 + 0.5 sin(2 pi 2 t): t=B+0.125 -> w=1 (polo de la pareja), t=B+0.375 -> w=0 (color propio)
    hi, lo = letter_state(p, 0, 1, B + 0.125, B, E), letter_state(p, 0, 1, B + 0.375, B, E)
    assert lo.color == p.color_from and hi.color == (0, 0, 0)
    assert abs(hi.dx) + abs(hi.dy) > 0.0 and lo.dx == lo.dy == 0.0
    assert letter_state(replace(p, osc_phase=math.pi), 0, 1, B + 0.125, B, E).color == p.color_from  # fase invertida
    r1, r2 = (letter_state(p, 0, 1, t, B, E, 0.2, True) for t in (0.125, 0.4))
    assert r1 == r2 and r1.color == (128, 100, 50) and r1.dx == r1.dy == 0.0  # congelado a w = 0.5
    assert letter_state(P(), 0, 1, 0.125, B, E).color == P().color_from  # sin osc: nada


def test_erode_shrinks_and_drifts_away():
    p = P(erode=1.0)
    mid = letter_state(p, 0, 1, E - 0.1, B, E)
    end = letter_state(p, 0, 1, E + 3, B, E)
    assert 0.8 < mid.scale < 1.0 and end.scale == pytest.approx(0.85) and end.alpha < 0.01
    assert (end.dx, end.dy) != (0.0, 0.0) and letter_state(p, 0, 1, E + 3, B, E, 0.0, True).dx == 0.0
    assert letter_state(P(erode=0.4), 0, 5, E + 3, B, E).scale == 1.0  # las que no se erosionan no cambian


def test_echoes_persist_and_freeze():
    assert echoes(P(), 3.0) == []
    p = P(trail=1.0)
    a, b = echoes(p, E + 5.0), echoes(p, E + 5.7)
    assert [e[2] for e in a] == pytest.approx([0.2, 0.1]) and a != b
    assert all(abs(dx) <= 0.025 + 1e-9 and abs(dy) <= 0.05 + 1e-9 for dx, dy, _ in a + b)
    assert echoes(p, 1.0, True) == echoes(p, 9.0, True) == [(0.0, 0.0, 0.2), (0.0, 0.0, 0.1)]


def test_strain_jitter_glow_and_gain():
    base = P(jitter_em=0.02, jitter_hz=6.0, strain_gain=1.0)
    a, b = letter_state(base, 1, 4, 1.5, B, E), letter_state(base, 1, 4, 1.5, B, E, strain=1.0)
    assert abs(b.dx) == pytest.approx(abs(a.dx) * 2.5) and b.dx != a.dx
    assert letter_state(base, 1, 4, 1.5, B, E, strain=0.0) == a
    quiet = P(strain_gain=1.0)  # sin jitter propio: aparece uno mínimo (<= 0.012 em)
    q = letter_state(quiet, 1, 4, 1.5, B, E, strain=1.0)
    assert 0.0 < abs(q.dx) <= 0.012 and letter_state(quiet, 1, 4, 1.5, B, E).dx == 0.0
    assert letter_state(quiet, 1, 4, 1.9, B, E, strain=1.0).glow == pytest.approx(0.25)
    assert letter_state(replace(base, strain_gain=0.0), 1, 4, 1.5, B, E, strain=1.0) == a  # gain 0 la ignora
    assert letter_state(base, 1, 4, 1.5, B, E, strain=5.0) == b  # se acota a 1
    r = [letter_state(quiet, 1, 4, t, B, E, strain=1.0, reduce=True) for t in (1.2, 1.7)]
    assert r[0] == r[1] and r[0].dx == r[0].dy == 0.0 and r[0].glow == pytest.approx(0.25)  # reduce: sin temblor, glow sí


def test_strain_render(fonts):
    fx = word(chromatic_em=0.02, strain_gain=1.0)
    assert render(fonts, fx, 1.5) == render(fonts, fx, 1.5, strain=0.0)  # por defecto, mismos píxeles
    assert render(fonts, fx, 1.5, strain=1.0) != render(fonts, fx, 1.5)  # más aberración
    assert render(fonts, fx, 1.5, strain=1.0) == render(fonts, fx, 1.5, strain=1.0)
    off = word(chromatic_em=0.02, strain_gain=0.0)
    assert render(fonts, off, 1.5, strain=1.0) == render(fonts, off, 1.5)
    assert render(fonts, word(strain_gain=1.0, glow=0.3), 1.5, strain=1.0) != render(fonts, word(strain_gain=1.0, glow=0.3), 1.5)
    js = word(strain_gain=1.0, jitter_em=0.02, jitter_hz=6)
    assert render(fonts, js, 1.3, reduce=True, strain=1.0) == render(fonts, js, 1.7, reduce=True, strain=1.0)  # sin temblor extra


def test_color_lerp():
    p = P()
    assert letter_state(p, 0, 5, 1.5, B, E).color == p.color_from
    assert letter_state(p, 4, 5, 1.5, B, E).color == p.color_to
    assert letter_state(p, 0, 1, 1.5, B, E).color == p.color_from


# ---------- render.py (skia raster) ----------

@pytest.fixture(scope="module")
def fonts():
    return Fonts()


def word(**kw) -> FxWord:
    m = Match(line=0, word=0, text="Perdido", lemma="perdido", lang="es", categories=("x",), begin_ms=1000, end_ms=2000)
    return FxWord(m, P(**kw))


def render(fonts, fx: FxWord, t: float, reduce=False, rms=0.0, strain=0.0) -> bytes:
    size = 60.0
    surf = skia.Surface(540, 200)
    surf.getCanvas().clear(skia.ColorTRANSPARENT)
    letters = letters_for(fonts, fx.match.text, size)
    draw_word_fx(surf.getCanvas(), fonts, fx, letters, 60, 120, size, t, rms, reduce, strain)
    return surf.makeImageSnapshot().tobytes()


def blank(b: bytes) -> bool:
    return not any(b)


CASES = {"perdido": PERDIDO, "tiempo": TIEMPO, "respirar": RESPIRAR, "love": LOVE, "grain": dict(grain=0.8),
         "fracture": dict(fracture=1.0), "sparkle": dict(sparkle=6, glow=0.5), "blur": dict(blur_em=0.2), "halo": dict(glow=0.3), "echo": dict(trail=1.0), "osc": dict(osc_hz=2.0, osc_color=(0, 0, 0))}


def test_letters_for_geometry(fonts):
    ls = letters_for(fonts, "Hola", 60)
    assert [L.ch for L in ls] == list("Hola") and ls[0].x == 0
    assert ls[2].x == pytest.approx(ls[1].x + ls[1].w) and ls[1].w > 0


@pytest.mark.parametrize("name", CASES)
def test_render_non_blank_and_pure(fonts, name):
    fx = word(**CASES[name])
    for t in (1.3, 1.8):
        a = render(fonts, fx, t)
        assert not blank(a)
        assert a == render(fonts, fx, t)  # idéntico en dos renders del mismo (t, params)


@pytest.mark.parametrize("name", ["perdido", "tiempo", "respirar", "grain", "fracture", "blur"])
def test_render_changes_with_time(fonts, name):
    fx = word(**CASES[name])
    assert render(fonts, fx, 1.2) != render(fonts, fx, 1.7)


def test_render_out_of_order_equal(fonts):
    fx = word(**PERDIDO, **RESPIRAR)
    a = render(fonts, fx, 1.5)
    render(fonts, fx, 0.2), render(fonts, fx, 3.0)
    assert render(fonts, fx, 1.5) == a


def test_echo_shimmer_after_word_and_osc_render(fonts):
    fx = word(trail=1.0)
    assert render(fonts, fx, 5.0) != render(fonts, fx, 5.9)  # tras asentarse sigue vibrando
    assert render(fonts, fx, 5.0, reduce=True) == render(fonts, fx, 5.9, reduce=True)
    ox = word(osc_hz=2.0, osc_color=(0, 0, 0), osc_jitter_em=0.05)
    assert render(fonts, ox, 1.125) != render(fonts, ox, 1.375) and render(fonts, ox, 1.2) == render(fonts, ox, 1.2)


def test_erode_fades_after_word(fonts):
    fx = word(erode=1.0, particles=20)
    assert not blank(render(fonts, fx, 1.5)) and blank(render(fonts, fx, 5.0))


def test_before_word_visible_without_entrance(fonts):
    assert not blank(render(fonts, word(drift_em=0.3), 0.0))
    assert blank(render(fonts, word(stagger_ms=50, letter_offset_em=0.2), 0.0))


def test_reduce_static_for_motion_only(fonts):
    fx = word(drift_em=0.4, jitter_em=0.05, jitter_hz=8, wave_em=0.1, wave_hz=2, scale_amp=0.2, rotation_deg=20,
              particles=10, sparkle=5, stagger_ms=100, letter_offset_em=0.3)
    a, b = render(fonts, fx, 1.2, reduce=True), render(fonts, fx, 1.8, reduce=True)
    assert a == b and not blank(a)
    assert render(fonts, fx, 1.2) != render(fonts, fx, 1.8)


@pytest.mark.parametrize("t", [-3.0, 0.0, 1.0, 1.5, 2.0, 2.4, 100.0])
@pytest.mark.parametrize("reduce", [False, True])
def test_extreme_values_no_exceptions(fonts, t, reduce):
    render(fonts, word(**EXTREME), t, reduce=reduce, rms=1.0)


@pytest.mark.parametrize("field,val", [(k, v) for k, v in EXTREME.items()])
def test_each_field_extreme(fonts, field, val):
    for t in (1.2, 2.2):
        render(fonts, word(**{field: val}), t, rms=0.9)


def test_single_letter_and_empty(fonts):
    m = Match(0, 0, "a", "a", "es", ("x",), 1000, 1000)  # palabra de duración cero
    fx = FxWord(m, P(**EXTREME))
    surf = skia.Surface(200, 200)
    draw_word_fx(surf.getCanvas(), fonts, fx, letters_for(fonts, "a", 60), 50, 120, 60, 1.0)
    draw_word_fx(surf.getCanvas(), fonts, fx, [], 50, 120, 60, 1.0)


def test_continuous_motion_waits_for_the_word_to_be_sung():
    p = P(jitter_em=0.05, jitter_hz=8.0, wave_em=0.05, wave_hz=2.0)
    for t in (0.0, B - 1.0, B - 0.01, B):
        s = letter_state(p, 1, 3, t, B, E)
        assert (s.dx, s.dy) == (0.0, 0.0), t  # nada de «saltito» antes de que la palabra llegue
    assert any(letter_state(p, 1, 3, B + 0.3 + k / 10, B, E).dy != 0.0 for k in range(5))


def test_each_letter_flashes_at_its_onset_and_returns_to_its_glow():
    p = P(glow=0.4)
    n, dur = 4, E - B
    for i in range(n):
        on = B + dur * i / n
        base = letter_state(p, i, n, on - 1.0, B, E).glow
        peak = letter_state(p, i, n, on, B, E).glow
        assert base == pytest.approx(0.4) and peak > base + 0.3  # destello
        assert letter_state(p, i, n, on + 0.5, B, E).glow == pytest.approx(base)  # vuelve a como estaba
    assert letter_state(p, 0, n, B, B, E, 0.0, True).glow == pytest.approx(0.4)  # reduced motion: sin destello


def test_letters_enter_and_flash_at_the_real_syllable_onsets():
    p = P(letter_offset_em=0.3, stagger_ms=140.0, glow=0.4)
    onsets = [1.0, 1.1, 1.8]  # tres letras: dos de una sílaba rápida y una de la siguiente
    for i, on in enumerate(onsets):
        assert letter_state(p, i, 3, on - 0.01, B, E, onset=on).alpha == 0.0  # no aparece antes de su sílaba
        s = letter_state(p, i, 3, on + 0.2, B, E, onset=on)
        assert s.alpha == 1.0 and s.dy == 0.0  # ya entró del todo, sin esperar al reparto fijo de stagger_ms
        assert letter_state(p, i, 3, on, B, E, onset=on).glow > 0.4 + 0.3  # destello en su propio onset


def test_eroded_letters_wait_for_the_next_verse_and_vanish_in_a_second():
    p = P(erode=1.0, particles=20, vanish_s=5.0)
    assert letter_state(p, 0, 1, E + 2.0, B, E).alpha == 1.0  # el verso sigue en pantalla: la letra se queda
    assert letter_state(p, 0, 1, 5.0, B, E).alpha == 1.0 and letter_state(p, 0, 1, 6.05, B, E).alpha < 0.01
    assert not [q for q in particles(p, [0.0], [0.6], 4.9, B, E) if q.kind == "dust"]  # nada de polvo antes
    assert [q for q in particles(p, [0.0], [0.6], 5.5, B, E) if q.kind == "dust"]
