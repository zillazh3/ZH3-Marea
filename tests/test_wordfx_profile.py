"""Perfil de ánimo: las cuatro canciones semilla como fixtures sintéticas (letras inventadas, audio hecho a mano)."""
import pytest

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import library
from marea.wordfx.fxtypes import AXES, AudioStats
from marea.wordfx.profile import (ARC_MOODS, TAGS, ArcSegment, ProfileError, SongProfile, classify, mood_at, mood_vector,
                                  vector_at)

LINE_MS = 4000


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def lyrics_of(texts: list[str], start=1000) -> Lyrics:
    """Un verso cada LINE_MS, una sílaba por palabra."""
    lines = []
    for i, text in enumerate(texts):
        ws = text.split()
        s = start + i * LINE_MS
        d = (LINE_MS - 400) // len(ws)
        syl = [Syllable(w, s + j * d, s + (j + 1) * d, word_end=j < len(ws) - 1) for j, w in enumerate(ws)]
        lines.append(Line(syl[0].begin, syl[-1].end, syllables=syl))
    return Lyrics(lines)


def check_arc(p: SongProfile, lyr: Lyrics):
    assert p.arc and all(s.mood in ARC_MOODS and s.start < s.end for s in p.arc)
    assert all(a.end == b.start for a, b in zip(p.arc, p.arc[1:]))  # contiguo y en orden
    assert p.arc[0].start == lyr.lines[0].start / 1000
    assert p.arc[-1].end == lyr.lines[-1].end / 1000


COMO_RESPIRAR = ["No puedo respirar me ahogo", "la tormenta me atrapa sin salida", "lloro de angustia y de miedo",
                 "grito y el dolor me rompe", "no hay aire solo pánico", "caigo y lloro otra vez"]
TE_LO_PROMETO = ["Te lo prometo mi mano en tu mano", "un abrazo mi familia mi casa", "aunque olvides mi nombre yo recuerdo",
                 "guardo la foto de aquel día", "tu memoria se desvanece", "pero el cariño se queda conmigo"]
AURORA = (["lloro de dolor", "el silencio y el vacío", "la soledad y la tristeza", "lágrimas en la tumba", "me hundo en el luto",
           "sufro la pena"]
          + ["recuerdo la foto", "el pasado y la infancia", "la memoria y el álbum", "la nostalgia de aquel día",
             "recordar el retrato", "el eco del ayer"]
          + ["feliz y alegre", "bailar y reír", "una sonrisa y una fiesta", "celebrar la alegría", "cantar con felicidad",
             "brindar y festejar"])
PARA_SIEMPRE = ["Estás lejos cuento la distancia", "espero tu regreso otra vez", "extraño tu voz y tu ausencia",
                "recuerdo el pasado aquel día", "vuelve pronto te espero", "la nostalgia me visita"]


def test_como_respirar_is_anguished_and_raw(lib):
    lyr = lyrics_of(COMO_RESPIRAR)
    p = classify(lyr, lib, AudioStats(energy=0.8, tempo_bpm=140, mode=-0.5, strain=0.85, brightness=0.6))
    assert {"sad", "anguished", "raw_vocals"} <= set(p.tags)
    assert p.intensity >= 0.75
    check_arc(p, lyr)
    assert {s.mood for s in p.arc} == {"anguished"} or p.arc[0].mood == "anguished"


def test_te_lo_prometo_is_tender_bittersweet_tribute(lib):
    lyr = lyrics_of(TE_LO_PROMETO)
    audio = AudioStats(energy=0.25, tempo_bpm=70, mode=0.2, strain=0.1)
    p = classify(lyr, lib, audio, notes="escrita para una familia cuyo padre tiene Alzheimer")
    assert {"tender", "bittersweet", "tribute", "nostalgic"} <= set(p.tags)
    assert not p.has("raw_vocals")
    assert p.intensity <= 0.5
    check_arc(p, lyr)


def test_aurora_arc_goes_from_sad_to_joyful(lib):
    lyr = lyrics_of(AURORA)
    p = classify(lyr, lib, AudioStats(energy=0.5, tempo_bpm=100), notes="homenaje a mis padres")
    assert {"tribute", "nostalgic", "bittersweet", "joyful"} <= set(p.tags)
    check_arc(p, lyr)
    moods = [s.mood for s in p.arc]
    assert len(moods) >= 2
    first_sad = next(i for i, m in enumerate(moods) if m in ("sad", "nostalgic", "longing"))
    last_joy = max(i for i, m in enumerate(moods) if m in ("joyful", "hopeful", "euphoric"))
    assert first_sad < last_joy
    assert mood_at(p, 5.0) in ("sad", "nostalgic", "longing") and mood_at(p, 60.0) in ("joyful", "hopeful", "euphoric")


def test_para_siempre_is_longing_no_raw(lib):
    lyr = lyrics_of(PARA_SIEMPRE)
    p = classify(lyr, lib, AudioStats(energy=0.4, tempo_bpm=95, mode=-0.3, strain=0.2))
    assert {"longing", "nostalgic"} <= set(p.tags)
    assert not p.has("raw_vocals")
    check_arc(p, lyr)


def test_classify_is_deterministic(lib):
    lyr = lyrics_of(AURORA)
    a = AudioStats(energy=0.5, tempo_bpm=100, mode=0.4)
    assert classify(lyr, lib, a, "homenaje") == classify(lyr, lib, a, "homenaje")


def test_empty_lyrics_is_neutral(lib):
    p = classify(Lyrics(lines=[]), lib, AudioStats(strain=0.9), notes="sad")
    assert p.tags == [] and p.arc == [] and p.intensity == 0.3


def test_notes_with_tag_names_force_them(lib):
    p = classify(lyrics_of(PARA_SIEMPRE), lib, notes="quiero que se sienta sad y con raw vocals")
    assert "sad" in p.tags and "raw_vocals" in p.tags


def test_round_trip_and_defaults(lib):
    p = classify(lyrics_of(AURORA), lib, notes="homenaje a mis padres", track_id="t1")
    assert SongProfile.from_dict(p.to_dict()) == p
    assert SongProfile.from_dict({}) == SongProfile()
    assert SongProfile.from_dict({"tags": ["sad", "calm", "sad"]}).tags == ["sad", "calm"]


@pytest.mark.parametrize("raw,msg", [
    ({"tags": ["happyish"]}, "Etiqueta desconocida"),
    ({"arc": [{"start": 10, "end": 20, "mood": "sad"}, {"start": 5, "end": 8, "mood": "joyful"}]}, "después"),
    ({"arc": [{"start": 0, "end": 10, "mood": "sad"}, {"start": 9, "end": 20, "mood": "calm"}]}, "solapar"),
    ({"arc": [{"start": 5, "end": 5, "mood": "sad"}]}, "menor"),
    ({"arc": [{"start": 0, "end": 5, "mood": "tribute"}]}, "ánimo desconocido"),
    ({"intensity": 1.5}, "intensity"),
    ({"intensity": True}, "intensity"),
    ({"notes": 3}, "texto"),
])
def test_validation_errors(raw, msg):
    with pytest.raises(ProfileError, match=msg):
        SongProfile.from_dict(raw)


def test_sad_is_cooler_and_heavier_than_joyful():
    t, g = AXES.index("temperature"), AXES.index("gravity")
    sad, joy = mood_vector("sad"), mood_vector("joyful")
    assert sad[t] < joy[t] and sad[g] > joy[g]
    assert mood_vector("tribute") == mood_vector("raw_vocals") == (0.0,) * len(AXES)
    assert all(len(mood_vector(m)) == len(AXES) for m in TAGS)


def test_vector_at_crossfade():
    p = SongProfile(arc=[ArcSegment(0, 10, "sad"), ArcSegment(10, 20, "joyful")])
    sad, joy = mood_vector("sad"), mood_vector("joyful")
    assert vector_at(p, 5.0) == pytest.approx(sad)
    assert vector_at(p, 15.0) == pytest.approx(joy)
    assert vector_at(p, 10.0) == pytest.approx([(a + b) / 2 for a, b in zip(sad, joy)])
    quarter = vector_at(p, 10.25)  # a medio fundido: 3/4 alegre
    assert quarter == pytest.approx([0.25 * a + 0.75 * b for a, b in zip(sad, joy)])


def test_vector_at_outside_arc_uses_global_tags_or_none():
    assert vector_at(SongProfile(), 3.0) is None
    assert vector_at(SongProfile(tags=["tribute"]), 3.0) is None
    p = SongProfile(tags=["sad", "joyful"], arc=[ArcSegment(10, 20, "calm")])
    mid = [(a + b) / 2 for a, b in zip(mood_vector("sad"), mood_vector("joyful"))]
    assert vector_at(p, 0.0) == pytest.approx(mid)
    assert vector_at(p, 15.0) == pytest.approx(mood_vector("calm"))
    assert mood_at(p, 0.0) is None and mood_at(p, 15.0) == "calm"
