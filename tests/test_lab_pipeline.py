"""Núcleo numérico del alineador con emisiones sintéticas (sin modelos ni torch) y recálculo parcial / offset del núcleo."""
import copy
import string

import numpy as np
import pytest

from marea.lab.alignment import (Alignment, AlignmentError, Line, Pause, Syl, Word, add_line_pauses, estimate_offset, line_from_dict,
                                 merge, validate)
from marea.lab.metrics import word_errors
from sidecar.pipeline import FRAME_MS, align_line, align_lines, frame_energy, viterbi

VOCAB = {c: i + 1 for i, c in enumerate(string.ascii_lowercase)}  # 0 = blank
V = len(VOCAB) + 1


def emissions(T, placements, peak=0.95):
    """Emisiones sintéticas: blank en todos los frames salvo donde se coloca una letra (frame, letra) o (rango de frames, letra)."""
    p = np.full((T, V), (1 - peak) / (V - 1))
    p[:, 0] = peak
    for where, ch in placements:
        for f in (range(*where) if isinstance(where, tuple) else [where]):
            p[f] = (1 - peak) / (V - 1)
            p[f, VOCAB[ch]] = peak
    return np.log(p / p.sum(1, keepdims=True))


def energy_with(T, loud, level=1.0, floor=0.02):
    e = np.full(T, floor)
    for a, b in loud:
        e[a:b] = level
    return e


def line_of(res, **kw) -> Line:
    return line_from_dict({**res, "id": "L0", **kw})


# ---------------- Viterbi ----------------

def test_viterbi_recovers_every_letter_and_holds_sustained_ones():
    logp = emissions(60, [(10, "h"), ((14, 24), "o"), (30, "l"), (36, "a")])
    spans = viterbi(logp, [VOCAB[c] for c in "hola"])
    assert [(s.first, s.last) for s in spans] == [(10, 10), (14, 23), (30, 30), (36, 36)]
    assert all(s.logp > np.log(0.9) for s in spans)


def test_viterbi_handles_a_whole_song_worth_of_letters():
    # con más de 127 estados (2·letras+1) el retroceso desbordaba int8 en NumPy 2; una canción real tiene miles
    text = ("abcdefghijklmnopqrstuvwxyz" * 12)[:300]
    frames = [(10 + 4 * i, c) for i, c in enumerate(text)]
    spans = viterbi(emissions(10 + 4 * len(text) + 20, frames), [VOCAB[c] for c in text])
    assert [s.first for s in spans] == [f for f, _ in frames]


def test_viterbi_needs_a_blank_between_equal_letters_and_rejects_too_short_audio():
    logp = emissions(40, [(5, "l"), (9, "l"), (20, "a")])
    spans = viterbi(logp, [VOCAB[c] for c in "lla"])
    assert [s.first for s in spans] == [5, 9, 20]
    held = viterbi(emissions(40, [((5, 15), "l"), (20, "a")]), [VOCAB[c] for c in "lla"])  # una «l» larga y dos letras «l»
    assert held[1].first > held[0].last + 1  # CTC no puede emitir dos veces seguidas la misma letra sin un blank en medio
    with pytest.raises(ValueError, match="demasiado corto"):
        viterbi(emissions(3, []), [VOCAB[c] for c in "lla"])  # l, blank, l, a: mínimo 4 frames
    assert len(viterbi(emissions(4, []), [VOCAB[c] for c in "lla"])) == 3


# ---------------- sílabas, palabras y honestidad de los tiempos ----------------

def test_without_energy_boundaries_come_from_letters_and_are_marked_approximate_never_split_evenly():
    # «mundo»: mun-do. La sílaba corta «mun» ocupa 10 frames desde su primera letra; «do» empieza mucho después
    logp = emissions(80, [(10, "m"), (13, "u"), (16, "n"), (50, "d"), (53, "o")])
    res = align_line(logp, [["mun", "do"]], VOCAB)
    a, b = res["words"][0]["syllables"]
    assert (a["start"], b["start"]) == (10 * 20, 50 * 20)  # las fronteras son las del audio, no la mitad del intervalo
    assert (a["end"], b["end"]) == (17 * 20, 54 * 20)  # sin evidencia de voz el fin no se estira…
    assert a["approximate"] and b["approximate"] and res["words"][0]["approximate"]  # …y se avisa
    assert res["pauses"] == []  # sin energía no se inventan pausas
    assert validate(Alignment(2000, [line_of(res)])) == []


def test_sustained_note_extends_only_while_there_is_voice_energy():
    logp = emissions(120, [(10, "h"), (13, "o"), (70, "l"), (73, "a")])
    energy = energy_with(120, [(9, 45), (69, 80)])  # «ho» suena hasta el frame 45; «la» hasta el 80
    res = align_line(logp, [["ho", "la"]], VOCAB, energy=energy)
    a, b = res["words"][0]["syllables"]
    assert a["end"] == 45 * 20 and not a["approximate"]  # estirada exactamente hasta que la energía cae
    assert b["end"] == 80 * 20 and not b["approximate"]
    assert a["end"] - a["start"] == 35 * 20  # una nota larga de verdad (700 ms), no los 20 ms de su última letra


def test_legato_note_is_cut_where_the_next_syllable_starts():
    logp = emissions(100, [(10, "h"), (13, "o"), (40, "l"), (43, "a")])
    res = align_line(logp, [["ho", "la"]], VOCAB, energy=energy_with(100, [(9, 70)]))  # la voz no para entre las dos
    a, b = res["words"][0]["syllables"]
    assert a["end"] == b["start"] == 40 * 20
    assert validate(Alignment(3000, [line_of(res)])) == []


def test_pause_needs_real_silence_of_at_least_150_ms():
    logp = emissions(140, [(10, "s"), (13, "i"), (60, "n"), (63, "o"), (100, "s"), (103, "i")])
    long_gap = align_line(logp, [["si"], ["no"], ["si"]], VOCAB, energy=energy_with(140, [(9, 20), (59, 68), (99, 108)]))
    # «si»→«no»: silencio de 20 a 59 (780 ms) = pausa; «no»→«si»: de 68 a 99 (620 ms) = pausa
    assert [(p["start"], p["end"], p["kind"]) for p in long_gap["pauses"]] == [(400, 1180, "intra_line"), (1360, 1980, "intra_line")]
    short_gap = align_line(logp, [["si"], ["no"], ["si"]], VOCAB, energy=energy_with(140, [(9, 28), (50, 94), (99, 108)]))
    # «no»→«si»: solo 5 frames (100 ms) de silencio antes de que vuelva la voz: no es pausa; «si»→«no» (440 ms) sí
    assert [(p["start"], p["end"]) for p in short_gap["pauses"]] == [(560, 1000)]
    assert validate(Alignment(3000, [line_of(long_gap)])) == []  # y las pausas no se solapan con texto activo


def test_text_outside_the_vocabulary_is_flagged_not_timed_by_guessing():
    logp = emissions(80, [(10, "a"), (20, "b"), (50, "c")])
    # «—» entre dos palabras: una sola unidad sin letras conocidas ocupa el hueco que dejan sus vecinas
    mid = align_line(logp, [["ab"], ["—"], ["c"]], VOCAB)
    dash = mid["words"][1]["syllables"][0]
    assert (dash["start"], dash["end"], dash["approximate"], dash["confidence"]) == (21 * 20, 50 * 20, True, 0.0)
    # sin hueco no hay de dónde sacar tiempo: largo cero y el validador exige que alguien lo fije a mano
    tight = align_line(emissions(30, [(5, "a"), (6, "b"), (7, "c")]), [["a"], ["…"], ["bc"]], VOCAB)
    assert tight["words"][1]["end"] == tight["words"][1]["start"]
    assert any("inicio" in e and "≥ fin" in e for e in validate(Alignment(2000, [line_of(tight)])))
    with pytest.raises(ValueError, match="vocabulario"):
        align_line(logp, [["123"]], VOCAB)


def test_accents_map_to_the_models_letters():
    logp = emissions(60, [(10, "c"), (13, "a"), (16, "n"), (19, "c"), (22, "i"), (25, "o"), (28, "n")])
    res = align_line(logp, [["can", "ción"]], VOCAB)
    assert [s["text"] for s in res["words"][0]["syllables"]] == ["can", "ción"] and res["words"][0]["text"] == "canción"


def test_confidence_reflects_how_sure_the_model_was():
    sure = align_line(emissions(40, [(5, "a")], peak=0.97), [["a"]], VOCAB)["words"][0]["confidence"]
    unsure = align_line(emissions(40, [(5, "a")], peak=0.45), [["a"]], VOCAB)["words"][0]["confidence"]
    assert sure > 0.9 > 0.6 > unsure


def test_spanish_vowel_is_the_nucleus_and_stays_approximate():
    # «ciu»: nucleo «iu» (diptongo); «mar»: núcleo «a» que termina donde empieza la «r»
    logp = emissions(100, [(10, "c"), (14, "i"), (16, "u"), (50, "m"), (54, "a"), (70, "r")])
    energy = energy_with(100, [(9, 40), (49, 85)])
    res = align_line(logp, [["ciu", "mar"]], VOCAB, energy=energy, lang="es")
    v1, v2 = [s["vowels"][0] for s in res["words"][0]["syllables"]]
    assert (v1["text"], v1["start"], v1["end"]) == ("iu", 14 * 20, 40 * 20)  # sin consonante después: hasta el fin de la sílaba
    assert (v2["text"], v2["start"], v2["end"]) == ("a", 54 * 20, 70 * 20)
    assert v1["approximate"] and v2["approximate"]
    en = align_line(logp, [["ciu", "mar"]], VOCAB, energy=energy, lang="en")
    assert all(s["vowels"] == [] for s in en["words"][0]["syllables"])  # la ortografía inglesa no es fonémica: no se inventan vocales


def test_word_level_output_for_languages_without_syllables():
    res = align_line(emissions(50, [(5, "a"), (9, "b")]), [["ab"]], VOCAB, syllables=False)
    assert res["words"][0]["syllables"] == [] and res["words"][0]["start"] == 100


# ---------------- merge ----------------

def aligned(n=4, **kw) -> Alignment:
    lines = []
    for i in range(n):
        t = 1000 * i
        lines.append(Line(t, t + 800, [Word("hey", t, t + 800, syllables=[Syl("hey", t, t + 800, id=f"L{i}.W0.S0", source="ctc")],
                                           id=f"L{i}.W0")], id=f"L{i}", **kw))
    return Alignment(10000, lines)


def recomputed(i, shift=0, **kw):
    t = 1000 * i + shift
    return Line(t, t + 700, [Word("hey", t, t + 700, syllables=[Syl("hey", t, t + 700, id=f"L{i}.W0.S0", source="ctc")], id=f"L{i}.W0")],
                id=f"L{i}", **kw)


def test_merge_replaces_only_the_recomputed_line():
    al = aligned()
    out = merge(al, ["L1"], [recomputed(1, 50)])
    assert [l.id for l in out.lines] == ["L0", "L1", "L2", "L3"]
    assert out.lines[1].start == 1050 and out.lines[1].end == 1750
    for i in (0, 2, 3):
        assert out.lines[i] == al.lines[i]
    assert al.lines[1].start == 1000  # no muta el original


def test_merge_can_split_one_line_into_two():
    out = merge(aligned(), ["L1"], [recomputed(1).__class__(1000, 1300, [Word("a", 1000, 1300, id="N0.W0")], id="N0"),
                                    Line(1400, 1800, [Word("b", 1400, 1800, id="N1.W0")], id="N1")])
    assert [l.id for l in out.lines] == ["L0", "N0", "N1", "L2", "L3"] and validate(out) == []


def test_merge_keeps_what_the_user_pinned_by_hand():
    al = aligned()
    al.lines[1].words[0].syllables[0].manual_override = True
    al.lines[1].words[0].syllables[0].start = 1100  # el usuario movió el inicio a mano
    al.lines[2].manual_override = True
    out = merge(al, ["L1", "L2"], [recomputed(1, 20), recomputed(2, 30)])
    syl = out.lines[1].words[0].syllables[0]
    assert syl.start == 1100 and syl.manual_override  # la sílaba fijada se conserva; el resto de la línea salió del recálculo
    assert out.lines[1].words[0].end == out.lines[1].end == 1800  # la sílaba fijada llega hasta 1800: su palabra y su línea crecen
    assert out.lines[2] == al.lines[2]  # línea fijada: se queda entera aunque se haya recalculado


def test_merge_refuses_results_that_break_the_neighbours_or_unknown_ids():
    al = aligned()
    with pytest.raises(AlignmentError, match="monótono"):
        merge(al, ["L1"], [recomputed(1, 1500)])  # quedaría empezando después que la línea que antes la seguía (L2)
    assert merge(al, ["L1"], [recomputed(1, 600)]).lines[1].start == 1600  # solaparse con la vecina sí se permite: así es el TTML real
    with pytest.raises(AlignmentError, match="no existen"):
        merge(al, ["L9"], [recomputed(1)])
    bad = copy.deepcopy(al)
    bad.lines[3].words[0].end = bad.lines[3].words[0].start  # un error que ya existía fuera del rango no bloquea el recálculo
    assert merge(bad, ["L1"], [recomputed(1, 10)]).lines[1].start == 1010


# ---------------- offset global y pausas entre líneas ----------------

def test_offset_is_the_median_delta_and_ignores_misaligned_lines():
    rng = np.random.default_rng(1)
    pairs = [(t, t + 300 + int(rng.integers(-15, 16))) for t in range(0, 40000, 2000)]
    pairs[3] = (pairs[3][0], pairs[3][0] + 2400)  # una línea mal alineada
    pairs[7] = (pairs[7][0], pairs[7][0] - 900)
    est = estimate_offset(pairs)
    assert abs(est["ms"] - 300) <= 15 and est["confidence"] >= 0.85 and est["method"] == "median_delta"


def test_offset_without_a_consistent_shift_has_no_confidence():
    scattered = estimate_offset([(i * 2000, i * 2000 + d) for i, d in enumerate([0, 700, -400, 1500, 200, -900, 1200, 50])])
    assert scattered["confidence"] < 0.5
    assert estimate_offset([(0, 100), (1000, 1100)]) == {"ms": 0, "confidence": 0.0, "method": "median_delta"}  # muy pocas líneas


def test_pauses_between_lines_are_added_only_when_the_line_end_is_measured():
    al = aligned()
    out = add_line_pauses(al)
    assert [(p.start, p.end, p.kind) for p in out.lines[0].pauses] == [(800, 1000, "between_lines")] and validate(out) == []
    al.lines[1].words[0].syllables[0].approximate = True
    assert add_line_pauses(al).lines[1].pauses == []  # el fin de esa línea es una suposición: el hueco también
    assert add_line_pauses(out).lines[0].pauses == out.lines[0].pauses  # idempotente


# ---------------- varias líneas a la vez, energía y métricas ----------------

def test_whole_song_alignment_locates_a_repeated_chorus_at_the_right_occurrence():
    # la misma línea «la» suena dos veces; la letra completa obliga a que la 1.ª ocurrencia del texto vaya a la 1.ª del audio
    logp = emissions(200, [(10, "l"), (13, "a"), (50, "s"), (53, "o"), (56, "l"), (120, "l"), (123, "a")])
    res = align_lines(logp, [[["la"]], [["sol"]], [["la"]]], VOCAB)
    assert [l["words"][0]["start"] for l in res] == [10 * 20, 50 * 20, 120 * 20]
    assert validate(Alignment(5000, [line_from_dict({**r, "id": f"L{i}"}) for i, r in enumerate(res)])) == []


def test_pauses_are_only_inside_lines_and_between_line_gaps_are_left_to_add_line_pauses():
    logp = emissions(200, [(10, "a"), (11, "b"), (100, "c"), (101, "d"), (150, "e")])
    energy = energy_with(200, [(9, 14), (99, 104), (149, 153)])
    res = align_lines(logp, [[["ab"], ["cd"]], [["e"]]], VOCAB, energy=energy)
    assert [(p["start"], p["end"]) for p in res[0]["pauses"]] == [(280, 1980)] and res[1]["pauses"] == []  # «cd»→«e» cruza líneas: no es de nadie aquí


def test_frame_energy_follows_the_voice_band_and_the_grid_matches_the_model():
    sr, secs = 16000, 3
    t = np.arange(sr * secs) / sr
    pcm = np.zeros_like(t, np.float32)
    pcm[sr:2 * sr] = 0.5 * np.sin(2 * np.pi * 1000 * t[sr:2 * sr])  # 1 kHz entre 1 y 2 s: dentro de la banda de la voz
    pcm += (0.5 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)  # un zumbido de 60 Hz todo el tiempo: fuera de la banda
    e = frame_energy(pcm)
    assert len(e) == (len(pcm) - 400) // 320 + 1  # exactamente los frames que da wav2vec2
    loud, quiet = e[55:95].mean(), np.concatenate([e[:45], e[105:]]).mean()
    assert loud > 20 * quiet  # el tono de 1 kHz domina; el zumbido grave casi no cuenta
    assert len(frame_energy(np.zeros(100, np.float32))) == 0


def test_word_errors_has_signed_bias_and_the_right_tolerances():
    truth = [(1000, 1500), (2000, 2500), (3000, 3500), (4000, 4500)]
    pred = [(1030, 1480), (2060, 2560), (3200, 3700), (3990, 4600)]
    m = word_errors(pred, truth)
    assert m["n"] == 4 and m["start"]["mae"] == round((30 + 60 + 200 + 10) / 4, 1)
    assert m["start"]["within50"] == 0.5 and m["start"]["within100"] == 0.75  # 30 y 10 ≤ 50; 60 ≤ 100; 200 no
    assert m["start"]["bias"] == 45.0  # mediana de [30, 60, 200, -10]: llega tarde
    assert m["end"]["bias"] == 80.0 and m["end"]["within100"] == 0.75  # errores de fin -20, +60, +200, +100: mediana 80; el de 100 cuenta (≤)
    early = word_errors([(a - 70, b - 70) for a, b in truth], truth)  # siempre temprano: el sesgo es negativo, el error absoluto no
    assert early["start"]["bias"] == -70.0 and early["start"]["mae"] == 70.0 and early["start"]["within100"] == 1.0
    with pytest.raises(ValueError):
        word_errors(pred[:3], truth)


# ---------------- sílabas por idioma y recorte de tramo ----------------

def test_syllables_never_change_the_text_and_only_spanish_is_split(monkeypatch):
    import sys
    import types

    from sidecar import syllables
    fake = types.SimpleNamespace(syllabify=lambda w: {"camino": ["ca", "mi", "no"], "ciudad": ["ciu", "dad"], "roto": ["xx", "yy"]}.get(w, [w]))
    monkeypatch.setitem(sys.modules, "silabeador", fake)
    monkeypatch.setattr(syllables, "_syllabify", fake.syllabify)
    assert syllables.syllabify_word("camino", "es") == ["ca", "mi", "no"]
    assert syllables.syllabify_word("¿ciudad?", "es") == ["¿ciu", "dad?"]  # los signos se pegan a la primera y a la última sílaba
    assert syllables.syllabify_word("roto", "es") == ["roto"]  # las sílabas no reconstruyen la palabra: no se confía
    assert syllables.syllabify_word("d'Artagnan", "es") == ["d'Artagnan"] and syllables.syllabify_word("123", "es") == ["123"]
    assert syllables.syllabify_word("camino", "en") == ["camino"] and syllables.syllabify_word("camino", "") == ["camino"]
    for text in ("¿ciudad? camino", "camino   ciudad"):
        assert " ".join("".join(w) for w in syllables.split_text(text, "es")) == " ".join(text.split())
    monkeypatch.setattr(syllables, "_syllabify", None)  # sin la librería: todo por palabra, sin romper nada
    assert syllables.split_text("camino ciudad", "es") == [["camino"], ["ciudad"]] and not syllables.available("es")


def test_windowed_realign_returns_times_on_the_songs_clock():
    from sidecar.pipeline import shift_times
    logp = emissions(100, [(10, "s"), (13, "i"), (60, "n"), (63, "o")])
    energy = energy_with(100, [(9, 20), (59, 70)])
    res = align_lines(logp, [[["si"], ["no"]]], VOCAB, energy=energy, lang="es")
    base = copy.deepcopy(res)
    out = shift_times(res, 5000)
    w0, w1 = out[0]["words"]
    assert out[0]["start"] == base[0]["start"] + 5000 == w0["start"] and w1["start"] == base[0]["words"][1]["start"] + 5000
    assert out[0]["pauses"][0]["start"] == base[0]["pauses"][0]["start"] + 5000
    assert w0["syllables"][0]["vowels"][0]["start"] == base[0]["words"][0]["syllables"][0]["vowels"][0]["start"] + 5000
    assert validate(Alignment(20000, [line_from_dict({**out[0], "id": "L0"})])) == []
