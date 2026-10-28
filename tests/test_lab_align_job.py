"""Modo A: del TTML original al sidecar y de vuelta (con un sidecar simulado: la prueba con el worker real está en test_lab_real.py)."""
import dataclasses
from pathlib import Path

import pytest

from marea.lab.alignment import Alignment, Line, Syl, Unit, Word, validate
from marea.lab.align_job import REVIEW_BELOW, apply_result, needs_review, request_lines
from marea.lab.ttml_in import from_ttml
from marea.lab.ttml_out import export_ttml
from marea.model.lyrics import parse_ttml

DEMO = Path(__file__).parent.parent / "assets" / "demo" / "marea-de-luz.ttml"
DUR = 54000


@pytest.fixture(scope="module")
def raw():
    return DEMO.read_bytes()


def fake_sidecar(al: Alignment, shift=lambda i: 300, conf=0.9) -> dict:
    """Lo que devolvería el worker: las mismas sílabas de las líneas principales con otros tiempos."""
    lines = []
    for i, l in enumerate(x for x in al.lines if not x.bg):
        d = dataclasses.asdict(l)
        s = shift(i)
        d["start"], d["end"] = l.start + s, l.end + s
        d.pop("ref"), d.pop("agent")  # el worker real devuelve el id de la línea y tiempos y texto; agente y referencias los pone la app
        for w in d["words"]:
            w["start"], w["end"], w["confidence"], w["approximate"] = w["start"] + s, w["end"] + s, conf, False
            w.pop("id"), w.pop("ref")
            for sy in w["syllables"]:
                sy["start"], sy["end"], sy["confidence"], sy["approximate"] = sy["start"] + s, sy["end"] + s, conf, False
                sy.pop("id"), sy.pop("ref")
        lines.append(d)
    return {"lines": lines, "language": "es", "audio_sha256": "ab" * 32, "duration_ms": DUR, "model": "fake/model", "device": {"device": "cpu"}}


def test_request_has_only_main_voice_text_in_syllables(raw):
    al = from_ttml(raw, DUR)
    req = request_lines(al)
    assert len(req) == sum(not l.bg for l in al.lines) == 11
    assert req[0] == {"id": "L0", "words": [["Ca", "mi", "no"], ["so", "lo"], ["por"], ["la"], ["ciu", "dad"]]}


def test_constant_shift_becomes_the_global_offset_and_the_export_returns_to_the_reference_clock(raw):
    al = from_ttml(raw, DUR)
    out = apply_result(al, fake_sidecar(al, lambda i: 300))
    assert out.global_offset["ms"] == 300 and out.global_offset["confidence"] == 1.0 and out.global_offset["method"] == "median_delta"
    assert out.audio_sha256 == "ab" * 32 and out.language == "es" and out.pipeline["aligner"] == "fake/model"
    assert validate(out) == []
    old, new = parse_ttml(raw), parse_ttml(export_ttml(out, raw))
    assert [(s.begin, s.end) for l in new.lines for s in l.syllables] == [(s.begin, s.end) for l in old.lines for s in l.syllables]


def test_ids_and_ttml_references_survive_so_merge_and_the_xml_rewrite_keep_working(raw):
    al = from_ttml(raw, DUR)
    out = apply_result(al, fake_sidecar(al, lambda i: 40))
    for o, n in zip(al.lines, out.lines):
        assert (o.id, o.ref, o.agent, o.bg) == (n.id, n.ref, n.agent, n.bg)
        if o.bg:
            continue  # la voz de fondo no se tocó: es la misma estructura
        assert [w.id for w in o.words] == [w.id for w in n.words]
        assert [(s.id, s.ref) for w in o.words for s in w.syllables] == [(s.id, s.ref) for w in n.words for s in w.syllables]


def test_background_voices_are_not_aligned_only_shifted_and_flagged_for_review(raw):
    al = from_ttml(raw, DUR)
    bg_before = [l for l in al.lines if l.bg]
    out = apply_result(al, fake_sidecar(al, lambda i: 300))
    bg_after = [l for l in out.lines if l.bg]
    assert bg_before and [(l.start + 300, l.end + 300) for l in bg_before] == [(l.start, l.end) for l in bg_after]
    assert all(l.approximate and l.confidence == 0.0 for l in bg_after)
    flagged = set(needs_review(out))
    assert all(s.id in flagged for l in bg_after for w in l.words for s in w.syllables)  # nada de la voz de fondo pasa como medido


def test_an_inconsistent_shift_is_not_applied_as_a_global_offset(raw):
    al = from_ttml(raw, DUR)
    out = apply_result(al, fake_sidecar(al, lambda i: (i * 937) % 2600 - 1300))  # cada línea se movió distinto
    assert out.global_offset["ms"] == 0 and out.global_offset["method"] == "median_delta_rejected" and out.global_offset["confidence"] < 0.7


def test_needs_review_flags_low_confidence_approximate_and_zero_length_units():
    ok = Line(0, 900, [Word("ok", 0, 900, confidence=0.9, id="w0")], id="L0")
    low = Line(1000, 1900, [Word("baja", 1000, 1900, confidence=REVIEW_BELOW - 0.01, id="w1")], id="L1")
    approx = Line(2000, 2900, [Word("aprox", 2000, 2900, confidence=0.9, approximate=True, id="w2")], id="L2")
    zero = Line(3000, 3900, [Word("cero", 3000, 3000, confidence=0.9, id="w3")], id="L3")
    syl = Line(4000, 4900, [Word("ab", 4000, 4900, confidence=0.9, id="w4", syllables=[Syl("a", 4000, 4400, confidence=0.9, id="s0"),
                                                                                         Syl("b", 4400, 4900, confidence=0.02, id="s1")])], id="L4")
    assert needs_review(Alignment(9000, [ok, low, approx, zero, syl])) == ["w1", "w2", "w3", "s1"]  # con sílabas, se juzga cada sílaba


def test_apply_result_leaves_untouched_the_lines_it_was_not_asked_about(raw):
    al = from_ttml(raw, DUR)
    data = fake_sidecar(al, lambda i: 100)
    data["lines"] = data["lines"][:3]  # solo L0..L2 se alinearon
    out = apply_result(al, data)
    before, after = {l.id: l for l in al.lines}, {l.id: l for l in out.lines}
    assert after["L0"].start == before["L0"].start + 100  # las pedidas sí cambiaron
    for lid in ("L5", "L9"):
        assert dataclasses.replace(after[lid], pauses=[]) == dataclasses.replace(before[lid], pauses=[])
