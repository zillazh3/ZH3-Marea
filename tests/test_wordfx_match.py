import json

import pytest
import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.engine.engine import _words
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import library
from marea.wordfx.library import LibraryError
from marea.wordfx.matcher import match_line, match_lyrics, word_groups
from marea.wordfx.plan import build_plan


def line_of(text: str, start=1000, per_word=400) -> Line:
    """Línea con una sílaba por palabra, cada una de `per_word` ms."""
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def test_bundled_library_loads_and_normalizes(lib):
    assert {"time", "loss", "love"} <= set(lib.categories)
    assert "adios" in lib.categories["loss"].lemmas["es"]  # «adiós» -> sin acento
    assert "manana" in lib.categories["time"].lemmas["es"]
    assert lib.index["es"]["perder"] == ["loss"]


@pytest.mark.parametrize("patch,msg", [
    ({"vector": {"valence": 2, "arousal": 0, "gravity": 0, "temperature": 0, "fragility": 0, "blur": 0}}, "valence"),
    ({"vector": {"valence": 0}}, "ejes"),
    ({"primitives": ["teleport"]}, "primitivos desconocidos"),
    ({"priority": 11}, "priority"),
    ({"lemmas": {"fr": ["a"]}}, "idioma"),
    ({"lemmas": {"es": []}}, "no tiene lemas"),
])
def test_invalid_library_gives_clear_error(patch, msg):
    cat = {"lemmas": {"es": ["x"]}, "vector": dict.fromkeys(library.AXES, 0.0), "primitives": ["glow"], "priority": 5}
    cat.update(patch)
    with pytest.raises(LibraryError, match=msg):
        library.parse({"c": cat})


def test_load_reports_broken_json(tmp_path):
    p = tmp_path / "lib.json"
    p.write_text("{no es json", encoding="utf-8")
    with pytest.raises(LibraryError, match="lib.json"):
        library.load(p)


def test_fue_tanto_el_tiempo_perdido(lib):
    line = line_of("Fue tanto el tiempo perdido")
    ms = match_line(line, 0, lib)
    assert [(m.text, m.lemma, m.lang, m.categories) for m in ms] == [
        ("tiempo", "tiempo", "es", ("time",)), ("perdido", "perder", "es", ("loss",))]
    assert (ms[0].begin_ms, ms[0].end_ms) == (1000 + 3 * 400, 1000 + 4 * 400)  # tiempo es la 4.ª palabra
    assert ms[1].word == 4


def test_words_group_like_the_engine():
    for line in exact_lyrics().lines:
        engine_words = [w.text for w in _words(line.syllables, False, 0)]
        assert ["".join(s.text for s in g) for g in word_groups(line.syllables)] == engine_words


def test_multi_syllable_word_timing_spans_all_syllables(lib):
    syls = [Syllable("per", 0, 300), Syllable("di", 300, 700), Syllable("do", 700, 2000, word_end=False)]
    (m,) = match_line(Line(0, 2000, syllables=syls), 0, lib)
    assert (m.text, m.lemma, m.begin_ms, m.end_ms) == ("perdido", "perder", 0, 2000)


def test_language_is_per_token_not_per_line():
    lib = library.parse({"c": {"lemmas": {"es": ["volver"], "en": ["again", "high"]}, "vector": dict.fromkeys(library.AXES, 0.0),
                               "primitives": ["glow"], "priority": 5}})
    ms = match_line(line_of("vuelvo a verte again"), 0, lib)
    assert [(m.lemma, m.lang) for m in ms] == [("volver", "es"), ("again", "en")]
    ms = match_line(line_of("estoy so high hoy"), 0, lib)
    assert [(m.lemma, m.lang) for m in ms] == [("high", "en")]


def test_density_limit_prefers_priority_then_hold(lib):
    # amor (7), perder (8) y tiempo (4): quedan dos; con la misma duración ganan perder y amor
    ms = match_line(line_of("tiempo perder amor"), 0, lib)
    assert [m.lemma for m in ms] == ["perder", "amor"]
    # si «tiempo» se sostiene mucho más, supera a «amor»
    line = line_of("tiempo perder amor")
    line.syllables[0].end = line.syllables[0].begin + 3000
    assert [m.lemma for m in match_line(line, 0, lib)] == ["tiempo", "perder"]
    assert len(match_line(line_of("tiempo perder amor"), 0, lib, max_fx=3)) == 3


def test_punctuation_and_unknown_words_do_not_match(lib):
    assert match_line(line_of("... ¡hola! mesa"), 0, lib) == []


def test_match_lyrics_keys_by_line_and_word(lib):
    lyrics = Lyrics([line_of("hola mundo"), line_of("Fue tanto el tiempo perdido", start=5000)])
    assert sorted(match_lyrics(lyrics, lib)) == [(1, 3), (1, 4)]


def test_plan_attaches_to_engine_words_and_overlay_renders(lib):
    lyrics = exact_lyrics()
    plan = build_plan(lyrics, lib)
    assert plan.matches, "la letra demo debería tener alguna palabra con efecto"
    scene = Scene(lyrics, demo_cover(), (270, 480), style=Style(), wordfx=plan, wordfx_debug=True)
    flagged = [(li, wi) for li, line in enumerate(lyrics.lines) for wi, w in enumerate(scene._by_line[id(line)].words) if w.fx]
    assert sorted(flagged) == sorted(plan.matches)
    # sin plan, ninguna palabra queda marcada y el frame es distinto con la depuración
    bare = Scene(lyrics, demo_cover(), (270, 480), style=Style())
    assert not any(w.fx for L in bare._by_line.values() for w in L.words)
    t_ms = lyrics.lines[min(k[0] for k in plan.matches)].start + 500
    px = lambda s: s.snapshot(t_ms).toarray(colorType=skia.kRGBA_8888_ColorType)
    assert (px(scene) != px(bare)).any()


def test_user_library_overrides_bundled(tmp_path, monkeypatch):
    user = tmp_path / "wordfx" / "library.json"
    user.parent.mkdir()
    user.write_text(json.dumps({"x": {"lemmas": {"es": ["sol"]}, "vector": dict.fromkeys(library.AXES, 0.0),
                                      "primitives": ["glow"]}}), encoding="utf-8")
    monkeypatch.setattr(library, "user_path", lambda: user)
    assert set(library.load().categories) == {"x"}
