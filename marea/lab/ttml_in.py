"""TTML -> Alignment conservando los índices de <p> y <span> (el parser de la app los pierde). Sirve de punto de partida del Modo A.

Cubre los `<p>` con spans con tiempo y sus voces de fondo. Un `<p>` con tiempo solo por línea no se importa: no hay tiempos de
palabra que conservar y repartirlos sería inventarlos; el alineador (etapa `align`) es quien le da palabras. Esos `<p>` quedan
intactos en el TTML exportado.
"""
import defusedxml.ElementTree as ET

from marea.lab.alignment import Alignment, Line, Syl, Word
from marea.lab.ttml_out import XML, _timed
from marea.model.lyrics import TT, TTM, parse_time


def _words(container, prefix: str, bg: bool = False) -> list[Word]:
    """Misma regla de palabras que `model.lyrics._timed_spans`: un espacio dentro o después del span cierra la palabra."""
    groups: list[list[Syl]] = [[]]
    for j, c in enumerate(_timed(container)):
        raw = "".join(c.itertext())
        if raw[:1].isspace() and groups[-1]:
            groups.append([])
        text = raw.strip()
        if text:
            groups[-1].append(Syl(text, parse_time(c.get("begin")), parse_time(c.get("end")), confidence=0.0, source="ttml",
                                  ref={"span_index": j}))
        if raw[-1:].isspace() or (c.tail or "").isspace():
            groups.append([])
    groups = [g for g in groups if g]
    if bg and groups:  # Apple envuelve la voz de fondo en "( … )"
        groups[0][0].text = groups[0][0].text.lstrip("(")
        groups[-1][-1].text = groups[-1][-1].text.rstrip(")")
        groups = [[s for s in g if s.text] for g in groups]
        groups = [g for g in groups if g]
    out = []
    for n, g in enumerate(groups):
        for k, s in enumerate(g):
            s.id = f"{prefix}.W{n}.S{k}"
        out.append(Word("".join(s.text for s in g), g[0].start, g[-1].end, confidence=0.0, source="ttml", id=f"{prefix}.W{n}", syllables=g))
    return out


def from_ttml(data: bytes, duration_ms: int) -> Alignment:
    root = ET.fromstring(data)
    lines = []
    for i, p in enumerate(root.iter(f"{TT}p")):
        words = _words(p, f"L{i}")
        agent = p.get(f"{TTM}agent") or "v1"
        if words:
            start = parse_time(p.get("begin")) if p.get("begin") else words[0].start
            end = parse_time(p.get("end")) if p.get("end") else words[-1].end
            lines.append(Line(start, end, words, agent=agent, confidence=0.0, source="ttml", id=f"L{i}", ref={"ttml_p_index": i}))
        bgs = [c for c in p if c.tag == f"{TT}span" and c.get(f"{TTM}role") == "x-bg"]
        for k, b in enumerate(bgs):
            bw = _words(b, f"L{i}b{k}", bg=True)
            if bw:
                lines.append(Line(parse_time(b.get("begin")) if b.get("begin") else bw[0].start,
                                  parse_time(b.get("end")) if b.get("end") else bw[-1].end, bw, agent=agent, bg=True, confidence=0.0,
                                  source="ttml", id=f"L{i}b{k}", ref={"ttml_p_index": i, "bg_index": k, "parent": f"L{i}"}))
    return Alignment(duration_ms, lines, root.get(f"{XML}lang") or "", source_lyrics={"kind": "ttml"})
