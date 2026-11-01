"""Import independent TTML performances with references to their original XML.

Timed syllables keep their segmentation. A line-only paragraph remains one
approximate text interval until alignment supplies measured subdivisions.
"""
import defusedxml.ElementTree as ET

from marea.lab.alignment import BG_ROLES, Alignment, Line, Syl, Word, assign_layers
from marea.lab.ttml_out import XML, _timed
from marea.model.lyrics import MAREA, TT, TTM, _plain_text, parse_time, spread


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
    root = ET.fromstring(data)
    lines = []
    for i, p in enumerate(root.iter(f"{TT}p")):
        words = _words(p, f"L{i}")
        bgs = [c for c in p if c.tag == f"{TT}span" and c.get(f"{TTM}role") == "x-bg"]
        agent = "" if p.get(f"{MAREA}agent-unknown") == "true" else (p.get(f"{TTM}agent") or "v1")
        p_role = p.get(f"{MAREA}role") or ""
        line_only = not words and bool(_plain_text(p)) and p.get("begin") is not None and p.get("end") is not None
        if line_only:
            words = [Word(_plain_text(p), parse_time(p.get("begin")), parse_time(p.get("end")),
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
