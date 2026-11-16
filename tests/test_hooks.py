"""Anzuelo: sugerencias de rangos (módulo puro) y encolado de variantes."""
import numpy as np
import pytest

from marea.audio.analysis import AudioAnalysis
from marea.demo_song import exact_lyrics
from marea.hooks.analyze import load_weights, normalize, repetition_maps, suggest
from marea.model.lyrics import Line, Lyrics, Syllable

LINE_MS, STEP = 3000, 3500  # cada verso dura 3 s y arranca cada 3.5 s


def lyrics_of(texts, start=2000, step=STEP, dur=LINE_MS, approx=False) -> Lyrics:
    lines = []
    for i, text in enumerate(texts):
        ws = text.split()
        s = start + i * step
        d = dur // len(ws)
        syl = [Syllable(w, s + j * d, s + (j + 1) * d, word_end=j < len(ws) - 1) for j, w in enumerate(ws)]
        lines.append(Line(syl[0].begin, syl[-1].end, syllables=syl, approx=approx))
    return Lyrics(lines)


VERSE = ["Caminé por calles vacías anoche", "buscando una luz que no encontré", "el viento me hablaba de ti",
         "y yo sin saber qué hacer", "cuentas los días que pasan lentos", "mientras el cielo se desvanece"]
CHORUS = ["Marea de luz llévame contigo", "hasta donde el mar se vuelve cielo", "no me sueltes nunca marea de luz",
          "que sin ti me pierdo en la corriente"]
BRIDGE = ["Un susurro en la madrugada", "una promesa que se rompe", "ya no queda nada de aquello", "solo cenizas en mi mano"]


def song() -> Lyrics:
    """verso · CORO · verso · CORO · puente · CORO: el coro (4 versos) se repite 3 veces."""
    return lyrics_of(VERSE[:4] + CHORUS + VERSE[4:] + CHORUS + BRIDGE + CHORUS)


def test_normalize_strips_accents_case_punctuation_and_adlibs():
    assert normalize("¡Marea DE luz, (ooh) llévame!") == "marea de luz llevame"


def test_repeated_block_is_detected_as_a_block_not_single_lines():
    texts = VERSE[:2] + CHORUS + VERSE[2:4] + CHORUS
    block, single = repetition_maps([normalize(t) for t in texts], 0.85)
    assert block[2] == {8} and block[8] == {2} and block[5] == {11}  # las 4 líneas del coro se emparejan una a una
    assert not block[0] and not single[0]


def test_known_chorus_block_ranks_first_for_the_matching_duration():
    res = suggest(song(), "Marea de luz", (15,), weights=load_weights(), outro_ms=500)
    best = res[15.0][0]
    chorus_starts = {4, 12, 20}  # donde empieza cada coro
    assert best.first in chorus_starts and best.last == best.first + 3
    assert "coro repetido" in best.explanation and "título" in best.explanation
    assert best.score > res[15.0][-1].score


def test_boundaries_duration_tolerance_and_preroll():
    ly, w = song(), load_weights()
    res = suggest(ly, "", (15, 30), weights=w, outro_ms=500)
    for target, sugg in res.items():
        assert sugg
        for s in sugg:
            assert s.start_ms == pytest.approx(max(0.0, ly.lines[s.first].start - w["preroll_ms"]))  # pre-roll sobre el primer verso
            assert s.end_ms == pytest.approx(ly.lines[s.last].end + 500)  # termina donde acaba el verso + outro
            assert w["tol_low"] * target * 1000 <= s.duration_ms <= w["tol_high"] * target * 1000


def test_suggestions_are_diversified():
    res = suggest(song(), "Marea de luz", (15,), weights=load_weights())
    out = res[15.0]
    assert len(out) >= 3
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            inter = max(0.0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))
            assert inter / min(a.duration_ms, b.duration_ms) <= 0.5 + 1e-9


def test_lyrics_only_has_lower_confidence_than_with_audio():
    ly = song()
    plain = suggest(ly, "Marea de luz", (15,), weights=load_weights())[15.0][0]
    rate = 100.0
    n = int(60 * rate)
    rms = np.full(n, 0.2, np.float32)
    for a in (4, 12, 20):  # el coro suena fuerte
        t0 = int((2 + a * 3.5) * rate)
        rms[t0:t0 + int(14 * rate)] = 1.0
    an = AudioAnalysis(rate, 60_000, rms, np.zeros(n, np.float32), np.zeros(n, np.float32), [], [], np.zeros(n, np.float32))
    with_audio = suggest(ly, "Marea de luz", (15,), analysis=an, weights=load_weights())[15.0][0]
    order = {"baja": 0, "media": 1, "alta": 2}
    assert order[with_audio.confidence] > order[plain.confidence]
    assert with_audio.first in {4, 12, 20} and "pico vocal" in with_audio.explanation


def test_audio_offset_shifts_where_the_audio_is_read():
    ly = lyrics_of(["uno dos tres cuatro cinco seis"] * 3 + ["otra frase distinta para el final largo"] * 3)
    rate, n = 100.0, 4000
    rms = np.zeros(n, np.float32)
    rms[int(2.5 * rate):int(12 * rate)] = 1.0  # el audio suena fuerte entre 2.5 y 12 s
    an = AudioAnalysis(rate, 40_000, rms, np.zeros(n, np.float32), np.zeros(n, np.float32), [], [], np.zeros(n, np.float32))
    base = suggest(ly, "", (10,), analysis=an, audio_offset_ms=0.0, weights=load_weights())[10.0][0]
    shifted = suggest(ly, "", (10,), analysis=an, audio_offset_ms=15000.0, weights=load_weights())[10.0][0]
    assert base.breakdown["energy"] > shifted.breakdown["energy"]


def test_line_only_lyrics_and_real_demo_song_work():
    approx = lyrics_of(VERSE + CHORUS + CHORUS, approx=True)
    out = suggest(approx, "Marea de luz", (15, 30), weights=load_weights())
    assert out[15.0] and out[30.0]
    demo = suggest(exact_lyrics(), "Marea de luz", (15, 30, 60), weights=load_weights())
    assert all(demo[t] for t in (15.0, 30.0, 60.0))


def test_empty_and_very_short_lyrics():
    assert suggest(Lyrics([]), "", (15,)) == {15.0: []}
    short = lyrics_of(["una sola línea cantada"])
    res = suggest(short, "", (60,), weights=load_weights())[60.0]
    assert len(res) == 1 and (res[0].first, res[0].last) == (0, 0)  # se amplía la ventana en vez de no devolver nada


def test_score_is_deterministic():
    a = suggest(song(), "Marea de luz", (15, 30), weights=load_weights())
    b = suggest(song(), "Marea de luz", (15, 30), weights=load_weights())
    assert a == b and [s.score for s in a[15.0]] == [s.score for s in b[15.0]]


def test_weights_json_is_clamped_and_unknown_keys_ignored(tmp_path):
    p = tmp_path / "w.json"
    p.write_text('{"repetition": 999, "title": -4, "mystery": 5, "top_n": "x", "preroll_ms": 300}', encoding="utf-8")
    w = load_weights(user=p)
    assert w["repetition"] == 10 and w["title"] == 0 and "mystery" not in w and w["top_n"] == 5 and w["preroll_ms"] == 300
    p.write_text("no es json", encoding="utf-8")
    assert load_weights(user=p)["repetition"] == 3.0  # el archivo roto se ignora
