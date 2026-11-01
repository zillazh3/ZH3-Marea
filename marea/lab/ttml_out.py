"""Export new TTML files while retaining independent vocal performances.

Reuse the original tree, preserving compatible metadata and untouched content.
Explicit corrections rebuild only the affected spans; unsafe metadata loss is
refused. Without a source, use the existing paragraph/x-bg conventions.
Vowels, pauses, provenance, and review history remain in alignment JSON.
"""
import io
import xml.etree.ElementTree as XET  # solo serializa: los archivos ajenos se parsean con defusedxml
from collections import Counter
from pathlib import Path

import defusedxml.ElementTree as ET

from marea.export.job import unique_path
from marea.lab.alignment import Alignment, AlignmentError, Line, Word, apply_offset, validate
from marea.model.lyrics import MAREA, MAREA_URI, TT, TTM, join_text, parse_time, parse_ttml

ITUNES_URI = "http://music.apple.com/lyric-ttml-internal"
ITUNES = f"{{{ITUNES_URI}}}"
XML = "{http://www.w3.org/XML/1998/namespace}"


def clock(ms: int) -> str:
    return f"{ms // 60000:02d}:{ms % 60000 / 1000:06.3f}"


def _register(namespaces: list[tuple[str, str]]):
    for prefix, uri in namespaces:
        XET.register_namespace(prefix, uri)


def _spans(words: list[Word], wrap: bool = False) -> list[XET.Element]:
    out = []
    for i, w in enumerate(words):
        for text, b, e in [(s.text, s.start, s.end) for s in w.syllables] or [(w.text, w.start, w.end)]:
            el = XET.Element(f"{TT}span", {"begin": clock(b), "end": clock(e)})
            el.text = text
            out.append(el)
        if i < len(words) - 1:
            out[-1].tail = " "
    if wrap and out:  # Apple envuelve la voz de fondo en "( … )"
        out[0].text, out[-1].text = "(" + out[0].text, out[-1].text + ")"
    return out


def _timed(container) -> list:
    return [c for c in container if c.tag == f"{TT}span" and not c.get(f"{TTM}role") and c.get("begin") is not None]


def _retime(container, line: Line, label: str):
    """Reescribe begin/end de los spans con tiempo de `container` según `span_index` de cada sílaba."""
    spans, syls = _timed(container), [s for w in line.words for s in w.syllables]
    if not syls:
        raise AlignmentError([f"{label}: sin sílabas que correspondan a los spans del TTML original"])
    done = set()
    for s in syls:
        j = s.ref.get("span_index")
        if j is None or not 0 <= j < len(spans):
            raise AlignmentError([f"{label}: la sílaba {s.text!r} no apunta a un span del TTML original"])
        spans[j].set("begin", clock(s.start))
        spans[j].set("end", clock(s.end))
        done.add(j)
    left = [c for j, c in enumerate(spans) if j not in done and "".join(c.itertext()).strip()]
    if left:  # dejarlo con su tiempo viejo mezclaría dos relojes en la misma línea
        raise AlignmentError([f"{label}: {len(left)} span(s) del TTML sin alinear (p. ej. {''.join(left[0].itertext())!r})"])


def _needs_marea_role(line: Line) -> bool:
    implied = "backing" if line.bg else ("duet" if line.agent != "v1" else "lead")
    return line.role != implied or line.role_confidence < 1.0 or not line.agent or line.bg


def _apply_role_attrs(el, line: Line, implied: str):
    if not line.agent:
        el.attrib.pop(f"{TTM}agent", None)
        el.set(f"{MAREA}agent-unknown", "true")
    if line.role != implied:
        el.set(f"{MAREA}role", line.role)
    if line.role_confidence < 1.0:
        el.set(f"{MAREA}role-confidence", f"{line.role_confidence:.3f}")


def _ensure_agent(root, agent: str):
    if not agent:
        return
    md = next(root.iter(f"{TT}metadata"), None)
    if md is None:
        head = root.find(f"{TT}head")
        if head is None:
            head = XET.Element(f"{TT}head")
            root.insert(0, head)
        md = XET.SubElement(head, f"{TT}metadata")
    if md is not None and not any(a.get(f"{XML}id") == agent for a in md.iter(f"{TTM}agent")):
        XET.SubElement(md, f"{TTM}agent", {"type": "person", f"{XML}id": agent})


def ensure_editable(original: bytes, line: Line):
    """Refuse edits that would silently discard unsupported inline markup."""
    if "ttml_p_index" not in line.ref:
        return
    p = list(ET.fromstring(original).iter(f"{TT}p"))[line.ref["ttml_p_index"]]
    if "bg_index" in line.ref:
        p = [s for s in p if s.get(f"{TTM}role") == "x-bg"][line.ref["bg_index"]]
    if any(list(s) for s in _timed(p)):
        raise AlignmentError(["This performance contains nested inline metadata; edit its source structure explicitly."])


def _replace_spans(container, line: Line, wrap: bool = False):
    old, new = _timed(container), _spans(line.words, wrap=wrap)
    if len(old) > len(new) and any(set(s.attrib) - {"begin", "end"} for s in old[len(new):]):
        raise AlignmentError(["Text correction would discard span metadata; preserve segmentation or edit individual syllables."])
    for previous, replacement in zip(old, new):
        replacement.attrib.update({k: v for k, v in previous.attrib.items() if k not in ("begin", "end")})
    for c in list(container):
        if c.tag == f"{TT}span" and not c.get(f"{TTM}role"):
            container.remove(c)
    container.text = None
    for i, el in enumerate(new):
        container.insert(i, el)


def _rewrite(original: bytes, al: Alignment) -> bytes:
    ns = [(p or "", u) for _, (p, u) in ET.iterparse(io.BytesIO(original), events=("start-ns",))]
    if any(_needs_marea_role(l) for l in al.lines) and not any(u == MAREA_URI for _, u in ns):
        ns.append(("marea", MAREA_URI))
    _register(ns)
    root = ET.fromstring(original)
    ps, by_p, added = list(root.iter(f"{TT}p")), {}, []
    for line in al.lines:
        if "ttml_p_index" not in line.ref:
            added.append(line)
            continue
        i = line.ref.get("ttml_p_index")
        if i is None or not 0 <= i < len(ps):
            raise AlignmentError([f"línea {line.id}: no apunta a un <p> del TTML original"])
        by_p.setdefault(i, []).append(line)
    for i, lines in by_p.items():
        p, rebuilt = ps[i], False
        p_agent = p.get(f"{TTM}agent") or "v1"
        for line in lines:
            label = f"línea {line.id}"
            edited = bool(line.ref.get("text_edited"))
            if "bg_index" in line.ref:
                bgs = [c for c in p if c.tag == f"{TT}span" and c.get(f"{TTM}role") == "x-bg"]
                k = line.ref["bg_index"]
                if not 0 <= k < len(bgs):
                    raise AlignmentError([f"{label}: la voz de fondo {k} no existe en el TTML original"])
                if edited:
                    raw_bg = "".join(bgs[k].itertext()).strip()
                    wrap = raw_bg.startswith("(") and raw_bg.endswith(")") or not raw_bg
                    _replace_spans(bgs[k], line, wrap=wrap)
                    rebuilt = True
                elif _timed(bgs[k]):
                    _retime(bgs[k], line, label)
                if (bgs[k].get("begin") is not None or edited or line.start != line.words[0].start
                        or line.end != line.words[-1].end):
                    bgs[k].set("begin", clock(line.start))
                    bgs[k].set("end", clock(line.end))
                orig_b_agent = bgs[k].get(f"{TTM}agent") or p_agent
                if line.agent != orig_b_agent:
                    if line.agent:
                        bgs[k].set(f"{TTM}agent", line.agent)
                        bgs[k].attrib.pop(f"{MAREA}agent-unknown", None)
                    else:
                        _apply_role_attrs(bgs[k], line, "backing")
                    _ensure_agent(root, line.agent)
                orig_b_role = bgs[k].get(f"{MAREA}role") or "backing"
                if line.role != orig_b_role:
                    bgs[k].set(f"{MAREA}role", line.role)
                if line.role_confidence < 1.0 or bgs[k].get(f"{MAREA}role-confidence") is not None:
                    bgs[k].set(f"{MAREA}role-confidence", f"{line.role_confidence:.3f}")
            else:
                if edited:
                    _replace_spans(p, line)
                    rebuilt = True
                elif _timed(p):
                    _retime(p, line, label)
                elif line.words and not (line.ref.get("line_only") and line.source == "ttml"):
                    for c in [c for c in p if not c.get(f"{TTM}role")]:
                        p.remove(c)
                    p.text = None
                    for k, el in enumerate(_spans(line.words)):
                        p.insert(k, el)
                    rebuilt = True
                if line.agent != p_agent:
                    if line.agent:
                        p.set(f"{TTM}agent", line.agent)
                        p.attrib.pop(f"{MAREA}agent-unknown", None)
                    else:
                        _apply_role_attrs(p, line, "lead")
                    _ensure_agent(root, line.agent)
                orig_p_role = p.get(f"{MAREA}role") or ("duet" if line.agent != "v1" else "lead")
                if line.role != orig_p_role:
                    p.set(f"{MAREA}role", line.role)
                if line.role_confidence < 1.0 or p.get(f"{MAREA}role-confidence") is not None:
                    p.set(f"{MAREA}role-confidence", f"{line.role_confidence:.3f}")
                if any(c.get(f"{TTM}role") == "x-bg" for c in p) and line.words:
                    if line.start != line.words[0].start or line.end != line.words[-1].end or p.get(f"{MAREA}begin") is not None:
                        p.set(f"{MAREA}begin", clock(line.start))
                        p.set(f"{MAREA}end", clock(line.end))
        if lines and (rebuilt or p.get("begin") is not None):
            p.set("begin", clock(min(l.start for l in lines)))
            p.set("end", clock(max(l.end for l in lines)))
    touched = {id(ps[i]) for i in by_p}
    rejected = al.source_lyrics.get("rejected") or []
    parent_map = {c: parent for parent in root.iter() for c in parent}
    for r in sorted(rejected, key=lambda r: (r.get("ttml_p_index", -1), r.get("bg_index", -1)), reverse=True):
        pi = r.get("ttml_p_index")
        if pi is None or not 0 <= pi < len(ps):
            continue
        p = ps[pi]
        if "bg_index" in r:
            bgs = [c for c in p if c.tag == f"{TT}span" and c.get(f"{TTM}role") == "x-bg"]
            bk = r["bg_index"]
            if 0 <= bk < len(bgs):
                p.remove(bgs[bk])
                touched.add(id(p))
        elif p in parent_map:
            parent_map[p].remove(p)
            if parent_map[p].tag == f"{TT}div":
                touched.add(id(p))
    if added:
        by_id = {l.id: l for l in al.lines if l.id}
        div0 = next(root.iter(f"{TT}div"), None)
        if div0 is None:
            div0 = XET.SubElement(next(root.iter(f"{TT}body")), f"{TT}div")
        for line in added:
            _ensure_agent(root, line.agent)
            parent = by_id.get(line.ref.get("parent", "")) if line.bg else None
            if line.bg and parent is not None and "ttml_p_index" in parent.ref:
                pp = ps[parent.ref["ttml_p_index"]]
                sp_attrs = {f"{TTM}role": "x-bg", "begin": clock(line.start), "end": clock(line.end)}
                if line.agent != (pp.get(f"{TTM}agent") or "v1"):
                    sp_attrs[f"{TTM}agent"] = line.agent
                sp = XET.SubElement(pp, f"{TT}span", sp_attrs)
                _apply_role_attrs(sp, line, "backing")
                sp.extend(_spans(line.words, wrap=True))
                if pp.get("begin") is not None:
                    pp.set("begin", clock(min(parse_time(pp.get("begin")), line.start)))
                    pp.set("end", clock(max(parse_time(pp.get("end")), line.end)))
                touched.add(id(pp))
            elif line.bg:
                if not line.ref.get("standalone"):
                    raise AlignmentError([f"voz de fondo {line.id!r} sin línea principal (ref.parent)"])
                pp = XET.SubElement(div0, f"{TT}p", {"begin": clock(line.start), "end": clock(line.end), f"{TTM}agent": line.agent})
                _apply_role_attrs(pp, line, "backing")
                sp = XET.SubElement(pp, f"{TT}span", {f"{TTM}role": "x-bg", "begin": clock(line.start), "end": clock(line.end)})
                _apply_role_attrs(sp, line, "backing")
                sp.extend(_spans(line.words, wrap=True))
                touched.add(id(pp))
            else:
                pp = XET.SubElement(div0, f"{TT}p", {"begin": clock(line.start), "end": clock(line.end), f"{TTM}agent": line.agent})
                _apply_role_attrs(pp, line, "duet" if line.agent != "v1" else "lead")
                pp.extend(_spans(line.words))
                touched.add(id(pp))
    for div in root.iter(f"{TT}div"):
        inner = list(div.iter(f"{TT}p"))
        times = [(parse_time(p.get("begin")), parse_time(p.get("end"))) for p in inner if p.get("begin") and p.get("end")]
        if times and div.get("begin") is not None and touched & {id(p) for p in inner}:
            div.set("begin", clock(min(b for b, _ in times)))
            div.set("end", clock(max(e for _, e in times)))
    for body in root.iter(f"{TT}body"):
        if body.get("dur") is not None:
            body.set("dur", clock(al.duration_ms))
    return XET.tostring(root, encoding="utf-8", xml_declaration=True)


def _generate(al: Alignment) -> bytes:
    ns = [("", TT[1:-1]), ("ttm", TTM[1:-1]), ("itunes", ITUNES_URI)]
    if any(_needs_marea_role(l) for l in al.lines):
        ns.append(("marea", MAREA_URI))
    _register(ns)
    attrs = {f"{ITUNES}timing": "Word"}
    if al.language:
        attrs[f"{XML}lang"] = al.language
    root = XET.Element(f"{TT}tt", attrs)
    md = XET.SubElement(XET.SubElement(root, f"{TT}head"), f"{TT}metadata")
    for agent in sorted({l.agent for l in al.lines if l.agent}):
        XET.SubElement(md, f"{TTM}agent", {"type": "person", f"{XML}id": agent})
    body = XET.SubElement(root, f"{TT}body", {"dur": clock(al.duration_ms)})
    main = [l for l in al.lines if not l.bg]
    div = XET.SubElement(body, f"{TT}div", {"begin": clock(min(l.start for l in al.lines)), "end": clock(max(l.end for l in al.lines))}
                         if al.lines else {})
    ids = {l.id for l in main if l.id}
    orphans = [l for l in al.lines if l.bg and l.ref.get("parent") not in ids and not l.ref.get("standalone")]
    if orphans:
        raise AlignmentError([f"voz de fondo {l.id!r} sin línea principal (ref.parent)" for l in orphans])
    entries = []
    for idx, line in enumerate(main):
        bgs = [b for b in al.lines if b.bg and b.ref.get("parent") == line.id and line.id]
        p_start = min([line.start] + [b.start for b in bgs])
        p_end = max([line.end] + [b.end for b in bgs])
        entries.append((p_start, idx, False, line, bgs, p_end))
    base = len(main)
    for idx, b in enumerate(l for l in al.lines if l.bg and l.ref.get("standalone") and l.ref.get("parent") not in ids):
        entries.append((b.start, base + idx, True, b, [b], b.end))
    for p_start, _, standalone, line, bgs, p_end in sorted(entries, key=lambda e: e[:2]):
        p = XET.SubElement(div, f"{TT}p", {"begin": clock(p_start), "end": clock(p_end), f"{TTM}agent": line.agent})
        if not standalone:
            _apply_role_attrs(p, line, "duet" if line.agent != "v1" else "lead")
            if bgs and (line.start != line.words[0].start or line.end != line.words[-1].end):
                p.set(f"{MAREA}begin", clock(line.start))
                p.set(f"{MAREA}end", clock(line.end))
            p.extend(_spans(line.words))
        elif not line.agent:
            p.attrib.pop(f"{TTM}agent", None)
            p.set(f"{MAREA}agent-unknown", "true")
        for b in bgs:
            sp_attrs = {f"{TTM}role": "x-bg", "begin": clock(b.start), "end": clock(b.end)}
            if b.agent != line.agent:
                sp_attrs[f"{TTM}agent"] = b.agent
            sp = XET.SubElement(p, f"{TT}span", sp_attrs)
            _apply_role_attrs(sp, b, "backing")
            sp.extend(_spans(b.words, wrap=True))
    return XET.tostring(root, encoding="utf-8", xml_declaration=True).replace(b"</p>", b"</p>\n")


def export_ttml(al: Alignment, original: bytes | None = None, *, allow_pending: bool = False) -> bytes:
    """Valida (con el offset global ya aplicado), genera el TTML y comprueba que el texto de cada capa sobrevive sin mezclarse."""
    if not allow_pending and any(line.review_state == "pending" for line in al.lines):
        raise AlignmentError(["Accept, correct, or reject pending vocal candidates before export (review required)."])
    applied = apply_offset(al)
    errs = validate(applied)
    if errs:
        raise AlignmentError(errs)
    out = _rewrite(original, applied) if original is not None else _generate(applied)
    parsed_out = parse_ttml(out)
    structural_edit = original is not None and (
        bool(applied.source_lyrics.get("rejected"))
        or any(l.ref.get("text_edited") or "ttml_p_index" not in l.ref for l in applied.lines)
    )
    if original is not None and not structural_edit:
        parsed_orig = parse_ttml(original)
        want_main = Counter(l.text for l in parsed_orig.lines if l.text)
        want_bg = Counter(join_text(b.syllables) for l in parsed_orig.lines for b in l.bg)
    else:
        want_main = Counter(l.text for l in applied.lines if not l.bg and l.text)
        want_bg = Counter(l.text for l in applied.lines if l.bg and l.text)
    got_main = Counter(l.text for l in parsed_out.lines if l.text)
    got_bg = Counter(join_text(b.syllables) for l in parsed_out.lines for b in l.bg)
    if want_main != got_main or want_bg != got_bg:
        diff = sorted(((want_main - got_main) | (got_main - want_main) | (want_bg - got_bg) | (got_bg - want_bg)))[:3]
        raise AlignmentError([f"el texto exportado no coincide con el original (p. ej. {diff})"])
    from marea.lab.ttml_in import from_ttml

    def signature(line):
        return (line.text, line.agent, line.bg, line.role, line.start, line.end,
                tuple((s.text, s.start, s.end) for s in line.leaves))

    expected = Counter(signature(l) for l in applied.lines)
    actual = Counter(signature(l) for l in from_ttml(out, applied.duration_ms).lines)
    if expected - actual or (original is None and actual != expected):
        raise AlignmentError(["TTML round trip changed a performance's agent, role, timing, or syllables."])
    return out


def write_new(al: Alignment, folder: str | Path, name: str, original: bytes | None = None) -> Path:
    """Exporta a `folder/name` o, si existe, a `name (2)`… La creación es exclusiva: no hay forma de pisar un archivo."""
    data = export_ttml(al, original)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = unique_path(folder, name)
    with open(path, "xb") as f:
        f.write(data)
    return path
