from pathlib import Path

import pytest

from marea.model.lyrics import join_text, parse_lrc, parse_time, parse_ttml
from marea.providers.lyrics.ttml_file import TtmlFileProvider

DEMO = Path(__file__).parent.parent / "assets" / "demo" / "marea-de-luz.ttml"
HEAD = ('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
        'xmlns:itunes="http://music.apple.com/lyric-ttml-internal"><body><div>')
TAIL = "</div></body></tt>"


@pytest.fixture(scope="module")
def demo():
    return TtmlFileProvider(DEMO).fetch()


def test_demo_lines_and_text(demo):
    assert len(demo.lines) == 11
    assert demo.lines[0].text == "Camino solo por la ciudad"
    assert demo.lines[8].text == "baja la marea, sube el corazón"


def test_demo_syllable_timing_and_word_end(demo):
    s = demo.lines[0].syllables
    assert [x.text for x in s] == ["Ca", "mi", "no", "so", "lo", "por", "la", "ciu", "dad"]
    assert (s[0].begin, s[0].end, s[-1].end) == (6000, 6333, 9000)
    assert [x.word_end for x in s] == [False, False, True, False, True, True, True, False, False]


def test_demo_sustained_syllable_weight(demo):
    # "mar~" pesa 4 de 13 unidades en 3.6 s -> ~1108 ms, contra ~277 ms de una sílaba normal
    last = demo.lines[1].syllables[-1]
    assert (last.text, last.begin, last.end) == ("mar", 11892, 13000)


def test_demo_duet_and_bg(demo):
    assert [l.agent for l in demo.lines].count("v2") == 2 and demo.lines[4].agent == "v2"
    bg = demo.lines[2].bg
    assert len(bg) == 1 and join_text(bg[0].syllables) == "sin palabras"  # sin los paréntesis de Apple
    assert demo.lines[2].end == bg[0].end == 17800


def test_demo_interludes(demo):
    assert [(i.start, i.end) for i in demo.interludes] == [(0, 6000), (28000, 33500)]


def test_ttml_formats_and_whitespace():
    ly = parse_ttml(HEAD + '<p begin="1:02.5" end="6.5s" ttm:agent="person2">'
                    '<span begin="1:02.500" end="1:03.000">Hel</span><span begin="1:03.000" end="1:04">lo </span>'
                    '<span begin="1:04" end="1:05"> you</span></p>' + TAIL)
    s = ly.lines[0].syllables
    assert ly.lines[0].start == 62500 and ly.lines[0].agent == "v1"
    assert [(x.text, x.word_end) for x in s] == [("Hel", False), ("lo", True), ("you", False)]
    assert parse_time("500ms") == 500 and parse_time("01:00:01.5") == 3601500


def test_ttml_line_timing_is_approx():
    ly = parse_ttml(HEAD + '<p begin="00:10.000" end="00:12.000">Un verso corto</p>' + TAIL)
    line = ly.lines[0]
    assert line.approx and [s.text for s in line.syllables] == ["Un", "verso", "corto"]
    assert line.syllables[0].begin == 10000 and line.syllables[-1].end == 12000


def test_ttml_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ttml("<html/>")
    with pytest.raises(ValueError):
        parse_ttml("no es xml")
    bomb = '<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><tt xmlns="http://www.w3.org/ns/ttml">&b;</tt>'
    with pytest.raises(ValueError):
        parse_ttml(bomb)


def test_lrc_spreads_by_letters_and_flags_approx():
    ly = parse_lrc("[ar:Alguien]\n[00:10.00]ab cdef\n[00:13.00]\n[00:20.00][00:30.00]hola\n")
    first = ly.lines[0]
    assert first.approx and first.end == 13000  # la línea vacía marca el fin
    a, b = first.syllables
    assert (a.begin, a.end, b.end) == (10000, 11000, 13000)  # 2 vs 4 letras
    assert [l.start for l in ly.lines] == [10000, 20000, 30000]
    assert ly.interludes[0].start == 0 and any(i.start == 13000 for i in ly.interludes)


def test_enhanced_lrc_keeps_per_word_and_per_syllable_timing():
    ly = parse_lrc("[00:12.00]<00:12.00>Ho<00:12.30>la <00:12.80>mundo<00:13.40>\n[00:15.00]<00:15.20>solo\n[00:18.00]plana\n")
    first, second, plain = ly.lines
    assert not first.approx and (first.start, first.end) == (12000, 13400)  # la etiqueta final sin texto cierra «mundo»
    assert [(s.text, s.begin, s.end, s.word_end) for s in first.syllables] == [
        ("Ho", 12000, 12300, False), ("la", 12300, 12800, True), ("mundo", 12800, 13400, False)]  # «Ho|la» es una sola palabra
    assert first.text == "Hola mundo"
    assert second.syllables[0].begin == 15200 and second.start == 15000 and not second.approx  # sin etiqueta de fin: hasta el siguiente verso o su tope
    assert plain.approx  # las líneas sin etiquetas de palabra siguen repartiéndose por letras
    assert ly.kind == "line"  # basta una línea aproximada para que la letra cuente como «por línea»


def test_enhanced_lrc_with_only_word_tags_is_fully_syllable_synced():
    ly = parse_lrc("[00:01.00]<00:01.00>a <00:01.50>b <00:02.00>\n")
    assert ly.kind == "syllable" and [(s.text, s.begin, s.end) for s in ly.lines[0].syllables] == [("a", 1000, 1500), ("b", 1500, 2000)]
