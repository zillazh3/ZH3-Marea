"""Sesión de revisión: edición validada, desfase aparte, recalcular una sección, vista previa y exportación (sin Qt)."""
import copy
import dataclasses
import hashlib

import pytest

from marea.lab.align_job import apply_result, from_text_result, text_request_lines
from marea.lab.alignment import Alignment, validate
from marea.lab.session import REALIGN_MARGIN_MS, Session
from marea.lab.ttml_in import from_ttml
from tests.test_lab_align_job import DEMO, DUR, fake_sidecar


@pytest.fixture
def raw():
    return DEMO.read_bytes()


@pytest.fixture
def sess(raw):
    al = from_ttml(raw, DUR)
    return Session(apply_result(al, fake_sidecar(al, lambda i: 0, conf=0.05)), raw)  # todo con confianza baja: todo por revisar


def first_syl(s: Session):
    return s.alignment.lines[0].words[0].syllables[0]


def test_editing_a_syllable_updates_its_word_and_line_marks_it_manual_and_clears_its_review(sess):
    syl = first_syl(sess)
    assert syl.id in sess.review_ids()
    word = sess.alignment.lines[0].words[0]
    assert sess.set_times(syl.id, syl.start + 20, syl.end - 20) == []
    syl = first_syl(sess)
    assert (syl.manual_override, syl.approximate, syl.confidence, syl.source) == (True, False, 1.0, "manual")
    assert syl.id not in sess.review_ids()  # ya lo revisó una persona
    assert sess.alignment.lines[0].words[0].start == syl.start and validate(sess.alignment) == []
    assert word.start < syl.start  # el objeto viejo sigue siendo el de antes (no se mutó en sitio sin validar)


def test_an_edit_that_would_break_the_alignment_is_refused_and_changes_nothing(sess):
    before = copy.deepcopy(sess.alignment)
    s0, s1 = sess.alignment.lines[0].words[0].syllables[:2]
    errors = sess.set_times(s0.id, s0.start, s1.end + 50)  # se mete encima de la sílaba siguiente
    assert errors and any("solapa" in e for e in errors)
    assert sess.alignment == before
    assert "debe ser menor" in sess.set_times(s0.id, 5000, 5000)[0] and "No existe" in sess.set_times("nada", 1, 2)[0]


def test_undo_restores_previous_states_one_by_one(sess):
    s = first_syl(sess)
    original = copy.deepcopy(sess.alignment)
    sess.set_times(s.id, s.start + 10, s.end)
    sess.set_offset(250)
    assert sess.undo() and sess.alignment.global_offset != {"ms": 250, "confidence": 1.0, "method": "manual"}
    assert sess.undo() and sess.alignment == original and not sess.undo()


def test_global_offset_is_independent_of_unit_times_and_applied_only_on_export(sess, raw, tmp_path):
    times_before = [(s.start, s.end) for l in sess.alignment.lines for w in l.words for s in w.syllables]
    sess.set_offset(120)
    assert [(s.start, s.end) for l in sess.alignment.lines for w in l.words for s in w.syllables] == times_before
    assert sess.alignment.global_offset == {"ms": 120, "confidence": 1.0, "method": "manual"}
    from marea.model.lyrics import parse_ttml
    exported = parse_ttml(sess.preview_lyrics_bytes()) if hasattr(sess, "preview_lyrics_bytes") else sess.preview_lyrics()
    assert exported.lines[0].start == sess.alignment.lines[0].start - 120  # el TTML queda en el reloj de la letra


def test_realign_request_uses_a_window_around_the_line_and_only_mode_b_may_change_text(sess):
    req = sess.realign_request("L3")
    line = sess.find("L3")[1]
    assert req["window"] == [max(0, line.start - REALIGN_MARGIN_MS), min(DUR, line.end + REALIGN_MARGIN_MS)]
    assert req["lines"][0]["id"] == "L3" and all(isinstance(w, list) for w in req["lines"][0]["words"])
    with pytest.raises(ValueError, match="TTML de origen"):
        sess.realign_request("L3", text="otra cosa")
    with pytest.raises(ValueError):
        sess.realign_request("L99")


def test_applying_a_realigned_section_keeps_what_the_user_pinned_and_everything_else(sess):
    pinned = sess.alignment.lines[5].words[0].syllables[0]
    assert sess.set_times(pinned.id, pinned.start + 30, pinned.end - 10) == []
    fixed = sess.find(pinned.id)[1]
    want = (fixed.start, fixed.end)
    others = {l.id: copy.deepcopy(l) for l in sess.alignment.lines if l.id not in ("L5",)}
    cur = sess.find("L5")[1]
    data = {"lines": [dict(dataclasses.asdict(cur), id="L5")]}
    for w in data["lines"][0]["words"]:
        for k in ("id", "ref"):
            w.pop(k, None)
            for s in w["syllables"]:
                s.pop(k, None)
        for s in w["syllables"]:
            s["confidence"], s["manual_override"] = 0.9, False
    data["lines"][0].pop("ref"), data["lines"][0].pop("agent")
    assert sess.apply_realign("L5", data) == []
    after = sess.find(pinned.id)[1]
    assert after.manual_override and (after.start, after.end) == want  # lo fijado a mano sigue exactamente igual
    assert all(sess.find(i)[1] == l for i, l in others.items() if i not in ("L5",) and not l.pauses)  # el resto, intacto
    assert sess.apply_realign("L5", {"lines": []}) == ["El resultado no trae la línea pedida."]


def test_text_mode_can_edit_text_and_gets_fresh_ids(tmp_path):
    data = {"lines": [{"id": "L0", "start": 1000, "end": 2000, "pauses": [], "words": [
        {"text": "hola", "start": 1000, "end": 1400, "confidence": 0.9, "approximate": False, "syllables": []},
        {"text": "mundo", "start": 1500, "end": 2000, "confidence": 0.9, "approximate": False, "syllables": []}]}],
        "language": "es", "audio_sha256": "ab" * 32, "duration_ms": 9000, "model": "m", "device": {"device": "cpu"}}
    s = Session(from_text_result(data), None)
    assert s.text_editable and s.find("L0.W1")[1].text == "mundo"
    req = s.realign_request("L0", text="hola a todos")
    assert req["lines"][0] == {"id": "L0", "text": "hola a todos"}
    new = {"lines": [{"id": "L0", "start": 1000, "end": 2600, "pauses": [], "words": [
        {"text": t, "start": a, "end": b, "confidence": 0.8, "approximate": False, "syllables": []}
        for t, a, b in (("hola", 1000, 1300), ("a", 1400, 1500), ("todos", 1600, 2600))]}]}
    assert s.apply_realign("L0", new) == [] and [w.text for w in s.alignment.lines[0].words] == ["hola", "a", "todos"]
    assert [w.id for w in s.alignment.lines[0].words] == ["L0.W0", "L0.W1", "L0.W2"]
    assert text_request_lines(["uno", "  ", "dos"]) == [{"id": "L0", "text": "uno"}, {"id": "L2", "text": "dos"}]


def test_preview_and_export_never_touch_the_original_file(sess, raw, tmp_path):
    orig = tmp_path / "cancion.ttml"
    orig.write_bytes(raw)
    digest = hashlib.sha256(orig.read_bytes()).hexdigest()
    assert len(sess.preview_lyrics().lines) == 11
    ttml, js = sess.export(tmp_path, "cancion.aligned.ttml")
    ttml2, js2 = sess.export(tmp_path, "cancion.aligned.ttml")
    assert ttml.name == "cancion.aligned.ttml" and ttml2.name == "cancion.aligned (2).ttml" and js.suffix == ".json" and js2 != js
    assert hashlib.sha256(orig.read_bytes()).hexdigest() == digest
    from marea.lab.alignment import load
    assert load(js).lines[0].words[0].syllables[0].text == "Ca"
