"""Esquema alignment.v1, validador y export TTML de la función experimental (núcleo puro, sin sidecar)."""
import copy
import hashlib
from pathlib import Path

import defusedxml.ElementTree as ET
import pytest

from marea.engine.engine import Engine
from marea.lab import alignment as A
from marea.lab.alignment import Alignment, AlignmentError, Line, Pause, Syl, Word, apply_offset, validate
from marea.lab.ttml_in import from_ttml
from marea.lab.ttml_out import export_ttml, write_new
from marea.model.lyrics import TT, parse_ttml

DEMO = Path(__file__).parent.parent / "assets" / "demo" / "marea-de-luz.ttml"
DUR = 54000
TIME_ATTRS = ("begin", "end", "dur")


@pytest.fixture(scope="module")
def demo_bytes():
    return DEMO.read_bytes()


@pytest.fixture
def demo_al(demo_bytes):
    return from_ttml(demo_bytes, DUR)


def shift(al: Alignment, ms: int) -> Alignment:
    out = copy.deepcopy(al)
    for line in out.lines:
        line.start, line.end = line.start + ms, line.end + ms
        for w in line.words:
            w.start, w.end = w.start + ms, w.end + ms
            for s in w.syllables:
                s.start, s.end = s.start + ms, s.end + ms
    return out


def untimed(el):
    """Estructura del XML sin los atributos de tiempo: lo que NO debe cambiar al re-sincronizar."""
    return (el.tag, {k: v for k, v in el.attrib.items() if k not in TIME_ATTRS}, el.text, el.tail, [untimed(c) for c in el])


def measure(line, active):
    return 60.0


def syl(text, b, e, **kw):
    return Syl(text, b, e, **kw)


def word(text, b, e, syls=None, **kw):
    return Word(text, b, e, syllables=syls or [], **kw)


def simple(**kw) -> Alignment:
    ls = [Line(1000, 3000, [word("Hola", 1000, 1800, [syl("Ho", 1000, 1400), syl("la", 1400, 1800)]), word("mundo", 2000, 3000)],
               id="L0")]
    return Alignment(10000, ls, language="es", **kw)


# ---------------- validador ----------------

def test_valid_alignments_have_no_errors(demo_al):
    assert validate(simple()) == []
    assert validate(demo_al) == []


def broken(fn):
    al = simple()
    fn(al)
    return validate(al)


@pytest.mark.parametrize("mutate, expected", [
    (lambda al: setattr(al.lines[0].words[0].syllables[0], "end", 1000), "inicio 1000 ≥ fin 1000"),
    (lambda al: setattr(al.lines[0].words[1], "end", 1900), "inicio 2000 ≥ fin 1900"),
    (lambda al: setattr(al.lines[0], "end", 12000), "fuera de la duración"),
    (lambda al: setattr(al.lines[0].words[0], "start", -5), "fuera de la duración"),
    (lambda al: setattr(al.lines[0].words[0].syllables[1], "start", 1300), "se solapa con la unidad anterior"),
    (lambda al: al.lines.append(Line(500, 900, [word("x", 500, 900)])), "orden no monótono"),
    (lambda al: setattr(al.lines[0].words[0].syllables[0], "text", "Hu"), "no reconstruyen el texto"),
    (lambda al: setattr(al.lines[0].words[0].syllables[0], "text", " "), "texto vacío"),
    (lambda al: al.lines[0].pauses.append(Pause(1500, 1600)), "pausa"),
    (lambda al: al.lines[0].pauses.append(Pause(1900, 1900)), "inicio 1900 ≥ fin 1900"),
])
def test_validator_rejects(mutate, expected):
    assert any(expected in e for e in broken(mutate)), broken(mutate)


def test_pause_between_words_is_valid_and_unit_is_not_active():
    al = simple()
    al.lines[0].pauses.append(Pause(1800, 2000))
    assert validate(al) == []


def test_other_voice_may_overlap_but_same_voice_must_be_monotonic():
    al = simple()
    al.lines.append(Line(900, 2500, [word("coro", 900, 2500)], agent="v2"))
    assert validate(al) == []


# ---------------- JSON ----------------

def test_json_round_trip_and_unknown_version(tmp_path, demo_al):
    demo_al.global_offset = {"ms": 40, "confidence": 0.8, "method": "detect_offset"}
    demo_al.lines[0].words[0].syllables[0].vowels.append(A.Vowel("a", 6000, 6100, approximate=True))
    A.save(demo_al, tmp_path / "a.json")
    back = A.load(tmp_path / "a.json")
    assert back == demo_al
    d = A.to_dict(demo_al)
    d["version"] = 2
    with pytest.raises(AlignmentError, match="no soportado"):
        A.from_dict(d)


# ---------------- offset ----------------

def test_apply_offset_shifts_every_level_and_zeroes_it(demo_al):
    demo_al.lines[0].words[0].syllables[0].vowels.append(A.Vowel("a", 6000, 6100))
    demo_al.lines[0].pauses.append(Pause(5000, 5500))
    demo_al.global_offset["ms"] = 250
    out = apply_offset(demo_al)
    assert out.offset_ms == 0 and demo_al.offset_ms == 250  # no muta el original
    assert out.lines[0].start == demo_al.lines[0].start - 250
    s0, o0 = out.lines[0].words[0].syllables[0], demo_al.lines[0].words[0].syllables[0]
    assert (s0.start, s0.vowels[0].start) == (o0.start - 250, 6000 - 250)
    assert out.lines[0].pauses[0].start == 4750


# ---------------- export con el TTML original ----------------

def test_rewrites_only_times_of_the_original(demo_bytes, demo_al):
    out = export_ttml(shift(demo_al, 250), demo_bytes)
    a, b = ET.fromstring(demo_bytes), ET.fromstring(out)
    assert untimed(a) == untimed(b)  # head, agentes, espacios, paréntesis de la voz de fondo… intactos
    old, new = parse_ttml(demo_bytes), parse_ttml(out)
    assert [l.text for l in new.lines] == [l.text for l in old.lines]
    for lo, ln in zip(old.lines, new.lines):
        assert ln.start == lo.start + 250 and ln.end == lo.end + 250
        assert [(s.begin, s.end, s.word_end) for s in ln.syllables] == [(s.begin + 250, s.end + 250, s.word_end) for s in lo.syllables]
        assert [(s.begin, s.end) for g in ln.bg for s in g.syllables] == [(s.begin + 250, s.end + 250) for g in lo.bg for s in g.syllables]
    root = ET.fromstring(out)
    assert next(root.iter(f"{TT}body")).get("dur") == "00:54.000"


def test_global_offset_is_removed_at_export(demo_bytes, demo_al):
    al = shift(demo_al, 300)
    al.global_offset = {"ms": 300, "confidence": 1.0, "method": "manual"}
    new, old = parse_ttml(export_ttml(al, demo_bytes)), parse_ttml(demo_bytes)
    assert [(s.begin, s.end) for l in new.lines for s in l.syllables] == [(s.begin, s.end) for l in old.lines for s in l.syllables]


def test_exported_ttml_loads_and_plays_in_the_engine(demo_bytes, demo_al):
    lyrics = parse_ttml(export_ttml(shift(demo_al, 120), demo_bytes))
    eng = Engine(lyrics, measure, 900)
    for t in (0, 7000, 20000, 52000):
        eng.warmup(t)
        eng.advance(1 / 60)
    assert eng.items


def test_invalid_alignment_never_produces_a_file(demo_bytes, demo_al, tmp_path):
    demo_al.lines[1].words[0].syllables[0].end = demo_al.lines[1].words[0].syllables[0].start
    with pytest.raises(AlignmentError):
        write_new(demo_al, tmp_path, "x.ttml", demo_bytes)
    assert not list(tmp_path.iterdir())


def test_write_new_never_overwrites(demo_bytes, demo_al, tmp_path):
    first = tmp_path / "cancion.aligned.ttml"
    first.write_bytes(b"original intacto")
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    path = write_new(demo_al, tmp_path, first.name, demo_bytes)
    assert path != first and path.name == "cancion.aligned (2).ttml"
    assert hashlib.sha256(first.read_bytes()).hexdigest() == digest
    assert parse_ttml(path.read_bytes()).lines


def test_bad_refs_are_errors_not_silent(demo_bytes, demo_al):
    demo_al.lines[0].ref["ttml_p_index"] = 99
    with pytest.raises(AlignmentError, match="no apunta a un <p>"):
        export_ttml(demo_al, demo_bytes)
    al = from_ttml(demo_bytes, DUR)
    al.lines[0].words[0].syllables[0].ref["span_index"] = 99
    with pytest.raises(AlignmentError, match="no apunta a un span"):
        export_ttml(al, demo_bytes)


def test_spans_left_unaligned_are_rejected_instead_of_mixing_clocks(demo_bytes, demo_al):
    demo_al.lines[0].words.pop()  # «dad» queda sin alinear pero sigue en el TTML
    with pytest.raises(AlignmentError, match="sin alinear"):
        export_ttml(demo_al, demo_bytes)


LINE_ONLY = ('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body dur="00:10.000"><div>'
             '<p begin="00:01.000" end="00:03.000">Hola <span ttm:role="x-translation">Hello</span></p></div></body></tt>').encode()


def test_line_timed_p_gets_real_word_spans_and_keeps_its_translation():
    al = Alignment(10000, [Line(1200, 2900, [word("Hola", 1200, 1700)], id="L0", ref={"ttml_p_index": 0})])
    out = export_ttml(al, LINE_ONLY)
    line = parse_ttml(out).lines[0]
    assert line.text == "Hola" and not line.approx and (line.syllables[0].begin, line.syllables[0].end) == (1200, 1700)
    assert (line.start, line.end) == (1200, 2900)
    assert b"Hello" in out and b'ttm:role="x-translation"' in out


def test_line_only_p_is_not_imported_nor_touched():
    al = from_ttml(LINE_ONLY, 10000)
    assert al.lines == []  # repartir su tiempo en palabras sería inventarlo


# ---------------- export sin original ----------------

def test_generated_ttml_has_spans_per_syllable_and_no_pauses():
    al = simple()
    al.lines[0].pauses.append(Pause(1800, 2000))
    out = export_ttml(al)
    root = ET.fromstring(out)
    assert len(list(root.iter(f"{TT}span"))) == 3  # Ho, la, mundo (palabra sin sílabas = un span); la pausa no se exporta
    line = parse_ttml(out).lines[0]
    assert line.text == "Hola mundo" and [(s.text, s.begin, s.end) for s in line.syllables] == [("Ho", 1000, 1400), ("la", 1400, 1800),
                                                                                                  ("mundo", 2000, 3000)]
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "es"


def test_generated_ttml_escapes_text_and_supports_duet_and_background():
    al = Alignment(10000, [
        Line(1000, 2500, [word("R&B", 1000, 1500), word("<3", 1600, 2000)], id="L0", agent="v1"),
        Line(2600, 4000, [word("yo", 2600, 3000, [syl("yo", 2600, 3000)])], id="L1", agent="v2"),
        Line(1200, 2400, [word("ooh", 1200, 2400)], id="L0b", bg=True, ref={"parent": "L0"}),
    ])
    lyr = parse_ttml(export_ttml(al))
    assert [l.text for l in lyr.lines] == ["R&B <3", "yo"]
    assert lyr.lines[1].agent == "v2" and [s.text for s in lyr.lines[0].bg[0].syllables] == ["ooh"]
    assert lyr.lines[0].end == 2500  # el fin de la línea incluye su voz de fondo


def test_background_without_parent_is_an_error():
    al = simple()
    al.lines.append(Line(1200, 1400, [word("eh", 1200, 1400)], bg=True, id="b"))
    with pytest.raises(AlignmentError, match="sin línea principal"):
        export_ttml(al)
