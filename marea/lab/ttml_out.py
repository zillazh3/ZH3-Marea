"""Alignment -> TTML nuevo. Nunca escribe sobre un archivo existente.

- Con el TTML original (`original=bytes`): se reutiliza su árbol y solo cambian los `begin`/`end` (de los `<span>`, de su `<p>`,
  `<div>` y `body dur`); head, traducciones, agentes, espacios y paréntesis quedan como estaban. Un `<p>` con tiempo solo por línea
  recibe spans nuevos. La serialización normaliza las declaraciones xmlns (las que nadie usa se pierden): equivalente, no byte a byte.
- Sin original: se genera con las convenciones de tools/export_demo_ttml.py (vocales y pausas no se exportan: el reproductor no
  tiene esa granularidad, quedan en el JSON).
"""
import io
import xml.etree.ElementTree as XET  # solo serializa: los archivos ajenos se parsean con defusedxml
from collections import Counter
from pathlib import Path

import defusedxml.ElementTree as ET

from marea.export.job import unique_path
from marea.lab.alignment import Alignment, AlignmentError, Line, Word, apply_offset, validate
from marea.model.lyrics import TT, TTM, parse_time, parse_ttml

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


def _rewrite(original: bytes, al: Alignment) -> bytes:
    _register([(p or "", u) for _, (p, u) in ET.iterparse(io.BytesIO(original), events=("start-ns",))])
    root = ET.fromstring(original)
    ps, by_p = list(root.iter(f"{TT}p")), {}
    for line in al.lines:
        i = line.ref.get("ttml_p_index")
        if i is None or not 0 <= i < len(ps):
            raise AlignmentError([f"línea {line.id}: no apunta a un <p> del TTML original"])
        by_p.setdefault(i, []).append(line)
    for i, lines in by_p.items():
        p, rebuilt = ps[i], False
        for line in lines:
            label = f"línea {line.id}"
            if "bg_index" in line.ref:
                bgs = [c for c in p if c.tag == f"{TT}span" and c.get(f"{TTM}role") == "x-bg"]
                k = line.ref["bg_index"]
                if not 0 <= k < len(bgs):
                    raise AlignmentError([f"{label}: la voz de fondo {k} no existe en el TTML original"])
                if _timed(bgs[k]):
                    _retime(bgs[k], line, label)
                if bgs[k].get("begin") is not None:
                    bgs[k].set("begin", clock(line.start))
                    bgs[k].set("end", clock(line.end))
            elif _timed(p):
                _retime(p, line, label)
            elif line.words:  # tiempo solo por línea: spans nuevos; se conservan los hijos con rol (voz de fondo, traducciones)
                for c in [c for c in p if not c.get(f"{TTM}role")]:
                    p.remove(c)
                p.text = None
                for k, el in enumerate(_spans(line.words)):
                    p.insert(k, el)
                rebuilt = True
        main = [l for l in lines if "bg_index" not in l.ref]
        if main and (rebuilt or p.get("begin") is not None):
            p.set("begin", clock(min(l.start for l in main)))
            p.set("end", clock(max(l.end for l in lines)))
    touched = {id(ps[i]) for i in by_p}
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
    _register([("", TT[1:-1]), ("ttm", TTM[1:-1]), ("itunes", ITUNES_URI)])
    attrs = {f"{ITUNES}timing": "Word"}
    if al.language:
        attrs[f"{XML}lang"] = al.language
    root = XET.Element(f"{TT}tt", attrs)
    md = XET.SubElement(XET.SubElement(root, f"{TT}head"), f"{TT}metadata")
    for agent in sorted({l.agent for l in al.lines}):
        XET.SubElement(md, f"{TTM}agent", {"type": "person", f"{XML}id": agent})
    body = XET.SubElement(root, f"{TT}body", {"dur": clock(al.duration_ms)})
    main = [l for l in al.lines if not l.bg]
    div = XET.SubElement(body, f"{TT}div", {"begin": clock(min(l.start for l in main)), "end": clock(max(l.end for l in al.lines))}
                         if main else {})
    ids = {l.id for l in main if l.id}
    orphans = [l for l in al.lines if l.bg and l.ref.get("parent") not in ids]
    if orphans:
        raise AlignmentError([f"voz de fondo {l.id!r} sin línea principal (ref.parent)" for l in orphans])
    for line in main:
        bgs = [b for b in al.lines if b.bg and b.ref.get("parent") == line.id and line.id]
        p = XET.SubElement(div, f"{TT}p", {"begin": clock(line.start), "end": clock(max([line.end] + [b.end for b in bgs])),
                                           f"{TTM}agent": line.agent})
        p.extend(_spans(line.words))
        for b in bgs:
            XET.SubElement(p, f"{TT}span", {f"{TTM}role": "x-bg"}).extend(_spans(b.words, wrap=True))
    return XET.tostring(root, encoding="utf-8", xml_declaration=True).replace(b"</p>", b"</p>\n")


def export_ttml(al: Alignment, original: bytes | None = None) -> bytes:
    """Valida (con el offset global ya aplicado), genera el TTML y comprueba que el texto de cada línea sobrevive."""
    applied = apply_offset(al)
    errs = validate(applied)
    if errs:
        raise AlignmentError(errs)
    out = _rewrite(original, applied) if original is not None else _generate(applied)
    want = Counter(l.text for l in (parse_ttml(original).lines if original is not None else [l for l in applied.lines if not l.bg]))
    got = Counter(l.text for l in parse_ttml(out).lines)
    if want != got:
        diff = sorted((want - got) | (got - want))[:3]
        raise AlignmentError([f"el texto exportado no coincide con el original (p. ej. {diff})"])
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
