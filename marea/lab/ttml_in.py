"""Import independent TTML performances with references to their original XML.

Timed syllables keep their segmentation. A line-only paragraph remains one
approximate text interval until alignment supplies measured subdivisions.
"""
import defusedxml.ElementTree as ET
from defusedxml import DefusedXmlException

from marea.lab.alignment import BG_ROLES, Alignment, Line, Syl, Word, assign_layers
from marea.model.lyrics import MAREA, TT, TTM, XML, iter_word_groups, parse_time, plain_text, spread


def _words(container, prefix: str, bg: bool = False) -> list[Word]:
    """Same word-grouping rule as `model.lyrics`: whitespace inside or trailing a span ends a word."""
    groups = iter_word_groups(container, bg=bg)
    out = []
    for n, g in enumerate(groups):
        syls = [
            Syl(text, b, e, confidence=0.0, source="ttml", id=f"{prefix}.W{n}.S{k}", ref={"span_index": j})
            for k, (j, text, b, e) in enumerate(g)
        ]
        out.append(Word("".join(s.text for s in syls), syls[0].start, syls[-1].end, confidence=0.0, source="ttml",
                        id=f"{prefix}.W{n}", syllables=syls))
    return out


def _spread_bg_words(span, prefix: str) -> list[Word]:
    """Voz de fondo `<span ttm:role="x-bg" begin end>` sin spans internos: conserva su texto y marca `approximate=True`."""
    if not (span.get("begin") and span.get("end")):
        return []
    text = " ".join("".join(span.itertext()).strip("() \n").split())
    if not text:
        return []
    b, e = parse_time(span.get("begin")), parse_time(span.get("end"))
    return [Word(s.text, s.begin, s.end, confidence=0.0, approximate=True, source="ttml", id=f"{prefix}.W{n}")
            for n, s in enumerate(spread(text, b, e))]


def _role_conf(el, fallback=None) -> float:
    raw = el.get(f"{MAREA}role-confidence")
    if raw is None and fallback is not None:
        raw = fallback.get(f"{MAREA}role-confidence")
    try:
        return max(0.0, min(1.0, float(raw))) if raw is not None else 1.0
    except ValueError:
        return 1.0


def from_ttml(data: bytes, duration_ms: int) -> Alignment:
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(f"El archivo no es un TTML válido: {e}") from e
    lines = []
    for i, p in enumerate(root.iter(f"{TT}p")):
        words = _words(p, f"L{i}")
        bgs = [c for c in p if c.tag == f"{TT}span" and c.get(f"{TTM}role") == "x-bg"]
        agent = "" if p.get(f"{MAREA}agent-unknown") == "true" else (p.get(f"{TTM}agent") or "v1")
        p_role = p.get(f"{MAREA}role") or ""
        text = plain_text(p)
        line_only = not words and bool(text) and p.get("begin") is not None and p.get("end") is not None
        if line_only:
            words = [Word(text, parse_time(p.get("begin")), parse_time(p.get("end")),
                          id=f"L{i}.W0", confidence=0.0, approximate=True, source="ttml")]
        if words:
            # Con x-bg en el mismo <p>, begin/end del <p> envuelven ambas capas: el verso principal conserva su propio rango.
            start = words[0].start if bgs else (parse_time(p.get("begin")) if p.get("begin") else words[0].start)
            end = words[-1].end if bgs else (parse_time(p.get("end")) if p.get("end") else words[-1].end)
            if p.get(f"{MAREA}begin") is not None:
                start, end = parse_time(p.get(f"{MAREA}begin")), parse_time(p.get(f"{MAREA}end"))
            lines.append(Line(start, end, words, agent=agent, role=p_role, role_confidence=_role_conf(p),
                              confidence=0.0, approximate=bool(line_only), source="ttml", id=f"L{i}",
                              ref={"ttml_p_index": i, **({"line_only": True} if line_only else {})}))
        for k, b in enumerate(bgs):
            bw = _words(b, f"L{i}b{k}", bg=True) or _spread_bg_words(b, f"L{i}b{k}")
            if bw:
                b_agent = "" if b.get(f"{MAREA}agent-unknown") == "true" else (b.get(f"{TTM}agent") or agent)
                b_role = b.get(f"{MAREA}role") or (p_role if not words and p_role in (*BG_ROLES, "uncertain") else "backing")
                b_rconf = _role_conf(b, p if not words else None)
                approx = any(w.approximate for w in bw)
                ref = {"ttml_p_index": i, "bg_index": k, "parent": f"L{i}"} if words else {"ttml_p_index": i, "bg_index": k, "standalone": True}
                lines.append(Line(parse_time(b.get("begin")) if b.get("begin") else bw[0].start,
                                  parse_time(b.get("end")) if b.get("end") else bw[-1].end, bw,
                                  agent=b_agent, bg=True, role=b_role, role_confidence=b_rconf,
                                  confidence=0.0, approximate=approx, source="ttml", id=f"L{i}b{k}", ref=ref))
    assign_layers(lines)
    return Alignment(duration_ms, lines, root.get(f"{XML}lang") or "", source_lyrics={"kind": "ttml"})
