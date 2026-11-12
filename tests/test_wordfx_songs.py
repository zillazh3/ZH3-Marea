import json

import pytest

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import library, songs
from marea.wordfx.plan import build_plan
from marea.wordfx.songs import Override, SongError


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def line_of(text: str, start=1000, per_word=400) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


LYRICS = Lyrics([line_of("Fue tanto el tiempo perdido")])  # tiempo = (0, 3), perdido = (0, 4)


def plan_with(lib, *overrides):
    return build_plan(LYRICS, lib, track_id="t1", overrides=list(overrides))


def test_disable_by_position_and_by_lemma(lib):
    assert sorted(plan_with(lib, Override(0, 3, disable=True)).words) == [(0, 4)]
    assert sorted(plan_with(lib, Override(lemma="perder", disable=True)).words) == [(0, 3)]


def test_force_categories_on_an_unmatched_word(lib):
    plan = plan_with(lib, Override(0, 1, categories=("loss", "nope")))  # «tanto»: no está en la biblioteca; «nope» no existe
    w = plan.get(0, 1)
    assert (w.match.text, w.match.categories, w.match.note) == ("tanto", ("loss",), "override")
    assert plan_with(lib, Override(0, 1, categories=("nope",))).get(0, 1) is None  # sin categorías válidas no se fuerza nada


def test_change_categories_of_a_matched_word(lib):
    w = plan_with(lib, Override(0, 4, categories=("love",))).get(0, 4)
    assert w.match.categories == ("love",) and w.match.lemma == "perder"


def test_vector_and_intensity_overrides_reach_the_composer(lib):
    base = plan_with(lib).get(0, 4).params
    w = plan_with(lib, Override(0, 4, vector={"arousal": 0.95}, intensity=0.0)).get(0, 4).params
    assert w.vector[1] == pytest.approx(0.95) and base.vector[1] != w.vector[1]
    assert w.erode == 0 and w.particles == 0  # intensidad 0 apaga los efectos de esa palabra


def test_more_specific_override_wins_over_lemma(lib):
    plan = plan_with(lib, Override(0, 4, categories=("love",)), Override(lemma="perder", categories=("loss",)))
    assert plan.get(0, 4).match.categories == ("love",)


def test_parse_errors_are_clear():
    assert songs.parse({}) == []  # un archivo solo con perfil no trae overrides
    for raw, msg in [([], "objeto"), ({"overrides": 5}, "lista"), ({"overrides": [1]}, "objeto"), ({"overrides": [{}]}, "line"),
                     ({"overrides": [{"lemma": "x", "vector": {"mood": 1}}]}, "vector"),
                     ({"overrides": [{"lemma": "x", "intensity": 9}]}, "intensity"),
                     ({"overrides": [{"lemma": "x", "categories": "loss"}]}, "categories")]:
        with pytest.raises(SongError, match=msg):
            songs.parse(raw)


def test_load_reads_the_track_file(tmp_path, monkeypatch, lib):
    monkeypatch.setattr(songs, "path_for", lambda tid: tmp_path / f"{tid}.json")
    assert songs.load("t1") == []
    (tmp_path / "t1.json").write_text(json.dumps({"overrides": [{"lemma": "tiempo", "disable": True}]}), encoding="utf-8")
    assert sorted(build_plan(LYRICS, lib, track_id="t1").words) == [(0, 4)]
    (tmp_path / "t1.json").write_text("{roto", encoding="utf-8")
    with pytest.raises(SongError, match="t1.json"):
        songs.load("t1")


def test_path_for_sanitizes_the_track_id():
    assert songs.path_for("../x/y").name == ".._x_y.json"
