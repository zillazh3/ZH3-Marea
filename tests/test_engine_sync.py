"""Intención de los ajustes de sincronía (no números): pausas entre sílabas, easing del wipe, notas sostenidas, letra por línea
y fin del interludio. A diferencia del golden, estos tests fallan si el comportamiento deseado se pierde."""
import pytest

from marea.engine import curves
from marea.engine.engine import LOOKAHEAD, SUSTAIN_LIFT, Engine
from marea.model.lyrics import Line, Lyrics, Syllable, parse_lrc


def measure(line, active):
    return 60.0


def syl(text, b, e, word_end=False):
    return Syllable(text, b, e, word_end)


def engine_at(lines, t_ms, **kw):
    eng = Engine(Lyrics(lines), measure, 900, **kw)
    eng.warmup(t_ms)
    eng.advance(0.0, running=False)  # evalúa los objetivos en el instante exacto (warmup termina un paso antes)
    return eng


def word_of(eng, idx=0):
    return eng.items[idx].words[0]


def one_line(*syllables):
    return [Line(syllables[0].begin, syllables[-1].end, syllables=list(syllables))]


# ---- pausas entre sílabas ----

SIENTO = one_line(syl("si", 1000, 1400), syl("ento", 2000, 2800))  # 0.6 s de pausa entre «si» y «ento»


def test_word_wipe_stops_in_the_pause_between_syllables():
    inside = [word_of(engine_at(SIENTO, t)).p for t in (1500, 1700, 1900)]
    assert inside == pytest.approx([2 / 6] * 3)  # «si» (2 de 6 letras) completa; «ento» aún no empieza
    assert word_of(engine_at(SIENTO, 2400)).p > 2 / 6 and word_of(engine_at(SIENTO, 2900)).p == pytest.approx(1.0)


def test_no_letter_is_lit_during_the_pause_and_letters_follow_their_own_syllable():
    w = word_of(engine_at(SIENTO, 1700))
    assert w.emph and all(c.S.g == curves.L_SCALE(0) and c.G.g == 0 for c in w.parts)  # todas en reposo
    mid_ento = word_of(engine_at(SIENTO, 2100)).parts  # a la mitad de la «e» (primera letra de «ento»), no de una letra de «si»
    assert max(range(6), key=lambda i: mid_ento[i].G.g) == 2  # las vecinas brillan menos, decayendo con la distancia
    b = word_of(engine_at(SIENTO, 1000)).parts
    assert [round(c.b, 3) for c in b] == [1.0, 1.2, 2.0, 2.2, 2.4, 2.6]  # letras repartidas dentro de cada sílaba


# ---- easing del wipe ----

def test_syllable_wipe_eases_in_and_out_but_stays_monotonic():
    line = one_line(syl("hola", 1000, 1800))  # 0.8 s: sin énfasis por letras
    p = {f: word_of(engine_at(line, 1000 + 800 * f)).parts[0].p for f in (0.1, 0.25, 0.5, 0.75, 0.9)}
    assert p[0.25] < 0.25 and p[0.75] > 0.75  # arranca lento y remata rápido
    assert p[0.5] == pytest.approx(0.5, abs=1e-6)
    assert list(p.values()) == sorted(p.values())
    assert p[0.5] - p[0.25] > p[0.25] - 0  # el tramo central va más rápido que el inicial


# ---- notas sostenidas ----

INDEPENDIENTE = one_line(syl("in", 1000, 1200), syl("de", 1200, 1400), syl("pen", 1400, 1600), syl("dien", 1600, 2900),
                         syl("te", 2900, 3000))  # 13 letras: nunca entra en el énfasis por letras


def test_sustained_syllable_rises_and_grows_more_than_a_short_one():
    short = word_of(engine_at(INDEPENDIENTE, 1000 + 0.9 * 200)).parts[0]
    held = word_of(engine_at(INDEPENDIENTE, 1600 + 0.9 * 1300)).parts[3]
    assert not word_of(engine_at(INDEPENDIENTE, 1000)).emph
    assert short.sus == 0 and held.sus > 0.9
    assert short.Y.g == pytest.approx(-1 / 60) and held.Y.g == pytest.approx(-1 / 60 * (1 + SUSTAIN_LIFT * held.sus))
    s_short = word_of(engine_at(INDEPENDIENTE, 1000 + 0.7 * 200)).parts[0].S.g
    s_held = word_of(engine_at(INDEPENDIENTE, 1600 + 0.7 * 1300)).parts[3].S.g
    assert s_short == pytest.approx(curves.SCALE(0.7)) and s_held > s_short + 0.04
    assert word_of(engine_at(INDEPENDIENTE, 1600 + 0.5 * 1300)).parts[3].G.g > word_of(engine_at(INDEPENDIENTE, 1000 + 0.1)).parts[0].G.g


# ---- letra por línea (sin sílabas reales) ----

LRC = parse_lrc("[00:01.00]aaaaaa bb ccc\n[00:09.00]fin\n")


def test_line_synced_lyrics_sweep_without_per_word_bounce_or_letter_emphasis():
    eng = engine_at(LRC.lines, 3000)
    L = eng.items[0]
    assert L.approx and not any(w.emph for w in L.words)  # «aaaaaa» dura > 1 s: con sílabas reales tendría énfasis por letras
    assert all(w.soft for w in L.words)
    for w in L.words:
        for u in w.parts:
            assert u.S.g == 1.0 and u.Y.g == 0.0  # sin saltos por palabra
    first, second = L.words[0], L.words[1]
    assert first.parts[0].b == pytest.approx(1.0)  # no empieza a llenarse antes de que la línea esté activa
    assert second.parts[0].b < first.parts[-1].e  # las palabras se solapan: un solo barrido continuo


def test_syllable_synced_lyrics_keep_their_exact_boundaries():
    eng = engine_at(SIENTO, 1000)
    assert not word_of(eng).soft
    eng = engine_at(one_line(syl("hola", 1000, 1500, True), syl("mundo", 1500, 2000)), 1000)
    assert [(u.b, u.e) for w in eng.items[0].words for u in w.parts] == [(1.0, 1.5), (1.5, 2.0)]  # sin solapes ni extensiones


# ---- interludio ----

GAP = [Line(1000, 2000, syllables=[syl("a", 1000, 2000)]), Line(9000, 10000, syllables=[syl("b", 9000, 10000)])]


def test_intro_dots_always_appear_from_1_2_seconds_and_fit_short_intros():
    from marea.model.lyrics import INTRO_MS
    assert INTRO_MS == 1200
    assert not [L for L in Engine(Lyrics(one_line(syl("a", 1100, 2000))), measure, 900).items if L.is_gap]  # <1.2 s: directo a la letra
    short = Engine(Lyrics(one_line(syl("a", 1500, 2500))), measure, 900)
    gap = short.items[0]
    assert gap.is_gap and gap.plan is not None and gap.speed > 1.2  # la animación completa se acelera para caber en 1.45 s
    short.warmup(350)  # el plan mínimo (1.9 s) corre ×1.3: la salida empieza a los ~690 ms reales
    mid = short.items[0]
    assert mid.phase in ("hold", "respira") and mid.grp_op > 0.5 and any(d.op > 0 for d in mid.dots)  # los puntos se ven
    short.warmup(1500 - LOOKAHEAD * 1000 - 2)
    last = short.items[short.active]
    assert last.is_gap and last.phase == "salida" and last.grp_op < 0.05  # y ya salieron cuando entra la primera línea
    long_ = Engine(Lyrics(one_line(syl("a", 6000, 7000))), measure, 900).items[0]
    assert long_.speed == 1.0  # una intro larga no se toca


def test_credit_is_a_last_item_that_never_becomes_active_and_scrolls_into_view():
    lines = [Line(1000, 2000, syllables=[syl("a", 1000, 2000)]), Line(3000, 4000, syllables=[syl("b", 3000, 4000)])]
    assert [L.kind for L in Engine(Lyrics(lines), measure, 900).items] == ["line", "line"]  # sin crédito, nada cambia
    eng = Engine(Lyrics(lines), measure, 900, credit_h=80.0)
    assert [L.kind for L in eng.items] == ["line", "line", "credit"]
    eng.warmup(10_000)  # mucho después del último verso
    assert eng.active == 1 and eng.items[eng.active].kind == "line"  # el crédito jamás es «la línea activa»
    credit, last = eng.items[2], eng.items[1]
    assert credit.h == 80.0 and credit.blur == 0 and credit.opacity == pytest.approx(1.0) and credit.s.p == pytest.approx(1.0)
    assert last.y.p + last.h <= credit.y.p + 1e-6 and credit.y.p + credit.h <= 900  # debajo del último verso y a la vista


def test_interlude_finishes_exactly_when_the_next_line_takes_over():
    eng = Engine(Lyrics(GAP), measure, 900)
    gap = next(L for L in eng.items if L.is_gap)
    assert gap.plan.total == pytest.approx(7000 - LOOKAHEAD * 1000)  # 7 s de hueco; la línea siguiente se activa 50 ms antes
    before = Engine(Lyrics(GAP), measure, 900)
    before.warmup(9000 - LOOKAHEAD * 1000 - 2)  # último instante con el interludio activo
    g = before.items[before.active]
    assert g.is_gap and g.phase == "salida" and g.grp_op < 0.05  # ya se desvaneció: no se corta a media salida
