"""Importación de .marea.json: datos mal formados se rechazan con ProjectError antes de tocar el estado del editor."""
import copy
import json

import pytest

from marea.model.lyrics import BgVocal, Line, Lyrics, Syllable
from marea.project import ProjectError, demo_project, project_from_dict, project_to_dict


@pytest.fixture
def raw():
    return json.loads(json.dumps(project_to_dict(demo_project())))  # lo mismo que leería del disco


def broken(raw, change):
    d = copy.deepcopy(raw)
    change(d)
    return d


def line0(d):
    return d["lyrics"]["lines"][0]


@pytest.mark.parametrize("change, hint", [
    (lambda d: line0(d).pop("start"), "start"),
    (lambda d: line0(d).pop("end"), "end"),
    (lambda d: line0(d)["syllables"][0].pop("text"), "text"),
    (lambda d: line0(d)["syllables"][0].pop("begin"), "begin"),
    (lambda d: line0(d).update(start="6000"), "start"),
    (lambda d: line0(d).update(start=True), "start"),
    (lambda d: line0(d)["syllables"][0].update(end=float("nan")), "end"),
    (lambda d: line0(d)["syllables"][0].update(end=float("inf")), "end"),
    (lambda d: line0(d).update(end=line0(d)["start"] - 1), "termina antes"),
    (lambda d: line0(d)["syllables"][0].update(text=5), "text"),
    (lambda d: line0(d).update(syllables="hola"), "syllables"),
    (lambda d: d["lyrics"]["lines"].__setitem__(0, "verso"), "verso 1"),
    (lambda d: d["lyrics"]["lines"][2]["bg"][0].pop("start"), "start"),
    (lambda d: d["lyrics"]["lines"][2]["bg"][0].update(end="x"), "end"),
    (lambda d: d.update(audio="nada"), "audio"),
    (lambda d: d["audio"].update(offset_ms="12"), "offset_ms"),
    (lambda d: d["audio"].update(offset_ms=float("inf")), "offset_ms"),
    (lambda d: d.update(version="uno"), "Versión"),
    (lambda d: d.update(track={"id": "t", "duration_ms": "largo"}), "duration_ms"),
])
def test_malformed_fields_are_rejected_with_a_clear_project_error(raw, change, hint):
    with pytest.raises(ProjectError, match=hint):
        project_from_dict(broken(raw, change))


def test_valid_round_trip_keeps_overlapping_lines_duets_and_backing_vocals():
    p = demo_project()
    syl = lambda t, b, e: Syllable(t, b, e, True)
    duet = Line(7000, 9500, "v2", [syl("dúo", 7000, 9500)], role="duet")  # se solapa con el primer verso
    bg = Line(8000, 12000, "v1", [syl("yo", 8000, 9000)], [BgVocal(9000, 12000, [syl("eco", 9000, 12000)], agent="v2")])
    p.lyrics = Lyrics(p.lyrics.lines + [duet, bg], p.lyrics.source, p.lyrics.credit, p.lyrics.writers)
    d = json.loads(json.dumps(project_to_dict(p)))
    back, _ = project_from_dict(d)
    assert back.lyrics.lines == p.lyrics.lines and (back.title, back.artist) == (p.title, p.artist)
    assert project_to_dict(back)["lyrics"] == d["lyrics"]
