"""Capa de movimiento por carácter (marea.engine.motion): intención, no números. Cresta anclada al wipe, adaptación al tempo
sin desfase, continuidad, tope de acumulación y reproducibilidad de la elección automática."""
import pytest

from marea.engine import curves, motion
from marea.engine.motion import PROFILES, Profile, bump, limit, pick, resolve, syllable
from marea.model.lyrics import Line, Syllable, parse_lrc

DT = 1 / 240


def trace(pf, b, e, n, sus=0.0, amt=1.0, dip=0.0):
    """Muestras (t, dy[n], ds[n]) de una sílaba, cada 1/240 s, desde antes de la ventana hasta después de la cola."""
    out, t = [], b - 0.6
    while t < e + 1.0:
        r = syllable(pf, amt, t, b, e, n, sus, dip, 0.0, 0.0)
        out.append((t, *(r or ([0.0] * n, [0.0] * n))))
        t += DT
    return out


def crest(samples, j):
    return max(samples, key=lambda s: -s[1][j])[0]


def test_wipe_inverse_is_the_inverse_of_wipe():
    for x in (0.05, 0.25, 0.5, 0.8, 0.95):
        assert curves.WIPE(curves.WIPE_INV(x)) == pytest.approx(x, abs=1e-5)


@pytest.mark.parametrize("b,e,n", [(1.0, 2.0, 4), (1.0, 1.3, 4), (1.0, 1.09, 6)], ids=["lenta", "normal", "extrema"])
def test_each_crest_lands_when_the_wipe_crosses_its_letter(b, e, n):
    s = trace(PROFILES["onda"], b, e, n)
    mid = (b + e) / 2
    stretch = min(motion.STRETCH_MAX, max(1.0, motion.STEP_MIN / ((e - b) / n)))
    for j in range(n):
        tc = b + (e - b) * curves.WIPE_INV((j + 0.5) / n)
        assert crest(s, j) == pytest.approx(mid + (tc - mid) * stretch, abs=1 / 60)
    assert [crest(s, j) for j in range(n)] == sorted(crest(s, j) for j in range(n))  # la onda recorre la palabra de izquierda a derecha


def test_fast_words_keep_a_wave_and_never_shift_the_syllable_more_than_a_stretch():
    b, e, n = 1.0, 1.09, 6  # 15 ms por letra: menos de un frame
    s = trace(PROFILES["onda"], b, e, n)
    first, last = crest(s, 0), crest(s, n - 1)
    assert last > first  # sigue habiendo recorrido
    assert b - 0.05 <= first and last <= e + 0.05  # el estiramiento es acotado: no se arrastra la onda lejos de la sílaba
    peak = max(-d for _, dy, _ in s for d in dy)
    slow = max(-d for _, dy, _ in trace(PROFILES["onda"], 1.0, 2.0, 4) for d in dy)
    assert 0.3 * slow < peak < slow  # menos amplitud que en lento, pero sin desaparecer


def test_amplitude_scales_with_time_per_letter_and_with_amt():
    peaks = []
    for e in (1.06, 1.15, 1.4):  # 6 letras
        peaks.append(max(-d for _, dy, _ in trace(PROFILES["onda"], 1.0, e, 6) for d in dy))
    assert 0 < peaks[0] <= peaks[1] + 1e-4 < peaks[2]  # 10 y 25 ms por letra comparten el piso; 67 ms da la amplitud completa
    half = max(-d for _, dy, _ in trace(PROFILES["onda"], 1.0, 2.0, 4, amt=0.5) for d in dy)
    full = max(-d for _, dy, _ in trace(PROFILES["onda"], 1.0, 2.0, 4) for d in dy)
    assert half == pytest.approx(full / 2, rel=0.12)  # casi lineal: el limitador comprime un poco las amplitudes grandes


def test_motion_is_continuous_and_returns_exactly_to_rest():
    for name, pf in PROFILES.items():
        for b, e, n in ((1.0, 2.0, 4), (1.0, 1.09, 6)):
            s = trace(pf, b, e, n, sus=1.0)
            for (_, dy0, ds0), (_, dy1, ds1) in zip(s, s[1:]):
                assert max(abs(a - c) for a, c in zip(dy0 + ds0, dy1 + ds1)) < 0.012, name  # sin saltos entre muestras de 4 ms
            assert all(v == 0.0 for v in s[0][1] + s[0][2] + s[-1][1] + s[-1][2]), name


def test_rebote_overshoots_below_baseline_and_onda_does_not():
    rebote = [d for _, dy, _ in trace(PROFILES["rebote"], 1.0, 1.4, 3) for d in dy]
    onda = [d for _, dy, _ in trace(PROFILES["onda"], 1.0, 1.4, 3) for d in dy]
    assert max(rebote) > 0.003 and max(onda) <= 1e-9  # dy > 0 = por debajo de la base


def test_breathing_only_on_sustained_notes_and_pulses():
    pf = PROFILES["respira"]
    held = [d for _, _, ds in trace(pf, 1.0, 3.0, 1, sus=1.0) for d in ds]
    short = [d for _, _, ds in trace(replace_profile(pf, lift=0, swell=0, enter=0), 1.0, 1.2, 1, sus=0.0) for d in ds]
    assert max(held) > 0.015 and min(held) < -0.015  # pulsa en ambos sentidos
    assert all(v == 0 for v in short)


def replace_profile(pf, **kw):
    from dataclasses import replace
    return replace(pf, **kw)


def test_limit_never_exceeds_cap_and_is_neutral_when_there_is_room():
    cap = motion.LIFT_CAP
    assert limit(-0.001, 0.0, cap) == pytest.approx(-0.001, rel=0.01)  # pequeño: casi intacto
    for base in (0.0, -0.04, -0.08, 0.05):
        for wave in (-0.3, -0.05, 0.05, 0.3):
            assert abs(base + limit(wave, base, cap)) <= cap + 1e-9
    assert limit(-0.05, -cap, cap) == 0.0  # ya al tope: la capa no suma
    assert limit(0.0, -0.05, cap) == 0.0


def test_stacked_effects_stay_under_the_caps():
    pf = Profile(lift=0.2, swell=0.2, settle=0.4, breath=0.2, enter=0.2)  # exagerado a propósito
    for t in (1.0 + i * DT for i in range(int(2.5 / DT))):
        r = syllable(pf, 1.5, t, 1.0, 3.0, 3, 1.0, motion.enter_dip(pf, 1.5, t, 1.0, 0), -0.06, 0.04)
        if r:
            assert all(abs(-0.06 + d) <= motion.LIFT_CAP + 1e-9 for d in r[0])
            assert all(abs(0.04 + d) <= motion.SCALE_CAP + 1e-9 for d in r[1])


def test_enter_cascades_by_word_and_ends_within_0_4_seconds():
    pf = PROFILES["onda"]
    assert motion.enter_dip(pf, 1.0, 0.99, 1.0, 0) == 0 and motion.enter_dip(pf, 1.0, 1.0 + 0.41, 1.0, 20) == 0
    peaks = [max(range(0, 400), key=lambda i: motion.enter_dip(pf, 1.0, 1.0 + i / 1000, 1.0, w)) for w in range(4)]
    assert peaks == sorted(peaks) and peaks[0] < peaks[3]
    assert all(motion.enter_dip(pf, 1.0, 1.0 + i / 1000, 1.0, w) < 1e-9 for w in (0, 9) for i in (400, 500))  # la última palabra termina a 0.4 s


def test_bump_has_bounded_support():
    for settle in (0.0, 0.35):
        assert bump(-1.0, 0.3, settle) == 0 and bump(2.0, 0.3, settle) == 0
        assert bump(0.0, 0.3, settle) == pytest.approx(1.0)


def line(*syls, approx=False):
    return Line(syls[0].begin, syls[-1].end, syllables=list(syls), approx=approx)


def test_pick_is_deterministic_and_follows_the_tempo():
    fast = line(*[Syllable("tata", i * 100, i * 100 + 100) for i in range(8)])  # 25 ms por carácter
    held = line(Syllable("hola", 0, 400), Syllable("mundo", 400, 2000))
    plain = line(Syllable("hola", 0, 600), Syllable("mundo", 600, 1200))
    assert pick(fast) == "onda" and pick(held) == "respira"
    assert pick(plain) == pick(line(Syllable("hola", 5000, 5600), Syllable("mundo", 5600, 6200))) in ("onda", "rebote")  # mismo texto, mismo perfil
    assert pick(line(Syllable("aaa", 0, 900), approx=True)) == "suave"
    assert {pick(line(Syllable(w, 0, 600))) for w in ("uno", "dos", "tres", "cuatro", "cinco", "seis")} == {"onda", "rebote"}  # hay variedad


def test_resolve_respects_mode_and_reduced_motion():
    assert resolve("auto", False, "rebote") is PROFILES["rebote"]
    assert resolve("onda", False, "rebote") is PROFILES["onda"]
    assert resolve("clasico", False, "rebote") is None and resolve("auto", True, "rebote") is None and resolve("onda", True, "x") is None
    assert resolve("auto", False, "") is None  # interludios y crédito no tienen perfil


def test_lrc_lines_get_the_soft_profile():
    assert all(pick(l) == "suave" for l in parse_lrc("[00:01.00]hola mundo\n[00:05.00]fin\n").lines)


# ---- integración con el motor ----

from marea.engine.engine import LOOKAHEAD, Engine, Params  # noqa: E402
from marea.model.lyrics import Lyrics  # noqa: E402


def measure(line, active):
    return 60.0


LINES = [Line(1000, 2200, syllables=[Syllable("hola", 1000, 1600, True), Syllable("mundo", 1600, 2200)]),
         Line(5000, 5600, syllables=[Syllable("otra", 5000, 5600)]),
         Line(9000, 9600, syllables=[Syllable("mas", 9000, 9600)]),
         Line(13000, 13600, syllables=[Syllable("fin", 13000, 13600)])]


def eng_at(t_ms, lines=LINES, **params):
    eng = Engine(Lyrics(lines), measure, 900, params=Params(**params))
    eng.warmup(t_ms)
    eng.advance(0.0, running=False)  # evalúa en el instante exacto
    return eng


def offsets(eng):
    return [[(u.wdy, u.wds) for w in L.words for u in w.parts] for L in eng.items if L.kind == "line"]


def moving(L):
    return [u for w in L.words for u in w.parts if u.wdy]


def line_item(eng, i):
    return [L for L in eng.items if L.kind == "line"][i]


def test_engine_applies_the_layer_while_singing_and_rests_before_and_long_after():
    assert moving(line_item(eng_at(1300), 0))  # «hola» suena: sus letras se mueven
    assert not moving(line_item(eng_at(500), 0))  # antes de la línea: nada (aunque sea la vecina)
    assert not any(moving(L) for L in eng_at(3500).items if L.kind == "line")  # la cola ya terminó y la siguiente aún no empieza
    far = eng_at(13100)  # la línea 0 está a tres de distancia: no se recalcula y debe estar en 0
    assert not moving(line_item(far, 0)) and not moving(line_item(far, 1))


def test_previous_line_tail_finishes_instead_of_freezing_when_it_leaves_the_active_range():
    for t in range(5000, 14000, 50):  # recorre todo: ninguna línea fuera de ±1 de la activa se queda con offsets
        eng = eng_at(t)
        for L in eng.items:
            if L.kind == "line" and abs(L.index - eng.active) > 1:
                assert not moving(L), (t, L.index)


def test_pause_keeps_offsets_and_seek_equals_continuous_play():
    eng = eng_at(1300)
    a = offsets(eng)
    for _ in range(30):
        eng.advance(1 / 60, running=False)  # pausa: el tiempo de la canción no corre
    assert offsets(eng) == a
    cont = Engine(Lyrics(LINES), measure, 900)
    cont.start(0)
    while cont.t < 1.3 - 1e-9:
        cont.advance(1 / 60)
    cont.seek(1300)
    cont.advance(0.0, running=False)
    for x, y in zip(sum(offsets(cont), []), sum(a, [])):
        assert x == y or (x[0] and y[0] and x[0] == pytest.approx(y[0], abs=1e-9))  # seguir tocando o saltar: mismo resultado
    back = eng_at(4000)
    back.seek(1300)  # retroceso
    back.advance(0.0, running=False)
    assert offsets(back) == a


def test_clasico_and_reduced_motion_leave_every_glyph_untouched():
    for kw in ({"motion": "clasico"}, {"reduce": True}, {"motion_amt": 0.0}):
        for t in range(900, 2600, 40):
            assert not any(moving(L) for L in eng_at(t, **kw).items if L.kind == "line"), (kw, t)


def test_reduced_motion_also_removes_syllable_bounce_and_vibrato():
    base, red = eng_at(1300), eng_at(1300, reduce=True)
    ys = lambda e: [u.y for u in line_item(e, 0).words[0].parts]
    assert any(ys(base)) and not any(ys(red))
    from marea.demo_song import exact_lyrics
    e = Engine(exact_lyrics(), measure, 900, params=Params(reduce=True))
    e.warmup(12400)
    assert all(c.x == 0 and c.scale == pytest.approx(1.0, abs=1e-3) for L in e.items for w in L.words if w.emph for c in w.parts)


def test_line_activation_ripples_through_the_words():
    eng = eng_at(5000 - LOOKAHEAD * 1000 + 100)  # 100 ms tras activarse «otra»
    assert any(d > 0 for u in moving(line_item(eng, 1)) for d in u.wdy)  # se hunde (dy > 0) al entrar


def test_classic_channels_are_untouched_by_the_layer():
    on, off = eng_at(1300), eng_at(1300, motion="clasico")
    for L1, L2 in zip(on.items, off.items):
        for w1, w2 in zip(L1.words, L2.words):
            for u1, u2 in zip(w1.parts, w2.parts):
                assert (u1.p, u1.y, u1.x, u1.scale, u1.glow_r, u1.glow_a) == (u2.p, u2.y, u2.x, u2.scale, u2.glow_r, u2.glow_a)


def test_emphasized_words_only_get_the_entry_dip():
    from marea.demo_song import exact_lyrics
    e = Engine(exact_lyrics(), measure, 900)
    e.warmup(12400)  # «mar~» suena: letras con énfasis
    letters = [c for L in e.items for w in L.words if w.emph for c in w.parts]
    assert letters and all(c.wdy is None for c in letters)  # ya pasó la entrada: ninguna onda en letras con énfasis


# ---- render ----

import numpy as np  # noqa: E402
import skia  # noqa: E402

from marea.demo_cover import demo_cover  # noqa: E402
from marea.demo_song import exact_lyrics  # noqa: E402
from marea.render.scene import Scene, Style  # noqa: E402
from marea.render.text import Fonts  # noqa: E402

STYLE = Style(title="", artist="", wm_text="")


def px(t_ms, **params):
    scene = Scene(exact_lyrics(), demo_cover(), (270, 480), params=Params(**params), style=STYLE)
    return scene.snapshot(t_ms).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)


def test_per_character_widths_add_up_to_the_syllable_width():
    f = Fonts()
    for text in ("ento", "Camino", "aquí", "mar"):
        assert sum(f.text(c, 70.0)[1] for c in text) == pytest.approx(f.text(text, 70.0)[1], abs=1e-6)


def test_drawing_letter_by_letter_with_no_offset_equals_the_fast_path():
    scene = Scene(exact_lyrics(), demo_cover(), (270, 480), params=Params(motion="clasico"), style=STYLE)
    scene.engine.warmup(12500)
    a = scene.snapshot  # noqa: F841 (la escena ya está asentada; se dibuja dos veces sobre el mismo estado)

    def draw():
        s = skia.Surface(270, 480)
        scene.render(s.getCanvas())
        return s.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)

    fast = draw()
    for L in scene.engine.items:
        for w in L.words:
            for u in w.parts:
                u.wdy, u.wds = [0.0] * len(u.text), [0.0] * len(u.text)
    assert np.abs(fast - draw()).max() <= 2  # mismo dibujo (tolerancia de redondeo de subpíxel)


def test_layer_changes_the_frame_only_while_it_moves():
    assert np.abs(px(13650) - px(13650, motion="clasico")).sum() > 0  # entra «y tu voz me llega…»: se ve la onda de entrada
    assert np.array_equal(px(13650), px(13650))  # determinista
    assert np.array_equal(px(0), px(0, motion="clasico"))  # al inicio no hay nada que mover
