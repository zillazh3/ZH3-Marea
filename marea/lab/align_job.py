"""Modo A: de un Alignment (p. ej. `from_ttml`) a la petición `align` del sidecar, y del resultado de vuelta a un Alignment."""
import copy

from marea.lab.alignment import Alignment, Line, add_line_pauses, estimate_offset, line_from_dict

MIN_OFFSET_CONFIDENCE = 0.7  # ponytail: provisional; por debajo no se aplica ningún desfase global (se pide calibración manual)
REVIEW_BELOW = 0.1  # ponytail: calibrado con 7 canciones de JamendoLyrics (2131 palabras): marca ~42 % y cubre ~90 % de las palabras con error > 500 ms


def request_lines(al: Alignment) -> list[dict]:
    """Líneas principales con texto, en el formato del sidecar: [{"id", "words": [[sílabas]]}]. Las voces de fondo no se alinean."""
    return [{"id": l.id, "words": [[s.text for s in w.syllables] or [w.text] for w in l.words]}
            for l in al.lines if not l.bg and l.words]


def remap_line(old: Line, res: dict) -> Line:
    """La línea alineada `res` (dict del sidecar) con la identidad de `old`: id, agente, referencia al TTML y los ids/referencias de cada
    palabra y sílaba (el sidecar no los conoce). Si el número de sílabas cambió, las sílabas nuevas quedan sin referencia."""
    new = line_from_dict({**res, "id": old.id, "agent": old.agent, "ref": old.ref, "source": "ctc"})
    old_s, new_s = [s for w in old.words for s in w.syllables], [s for w in new.words for s in w.syllables]
    if old_s and len(old_s) == len(new_s):
        for o, n in zip(old_s, new_s):
            n.id, n.ref = o.id, o.ref
    for o, n in zip(old.words, new.words):
        n.id = o.id
    ws = [w.confidence for w in new.words]
    new.confidence, new.approximate = sum(ws) / len(ws), any(w.approximate for w in new.words)
    return new


def text_request_lines(texts: list[str]) -> list[dict]:
    """Modo B: el texto (ya corregido) línea por línea; el sidecar lo parte en palabras y, en español, en sílabas."""
    return [{"id": f"L{i}", "text": t} for i, t in enumerate(texts) if t.strip()]


def from_text_result(data: dict, kind: str = "text") -> Alignment:
    """Modo B: Alignment nuevo a partir del resultado del sidecar (sin TTML de origen: no hay referencias ni desfase de referencia)."""
    lines = []
    for r in data["lines"]:
        line = line_from_dict({**r, "source": "ctc"})
        for n, w in enumerate(line.words):
            w.id = f"{line.id}.W{n}"
            for k, s in enumerate(w.syllables):
                s.id = f"{w.id}.S{k}"
        ws = [w.confidence for w in line.words]
        line.confidence, line.approximate = sum(ws) / len(ws), any(w.approximate for w in line.words)
        lines.append(line)
    al = Alignment(data["duration_ms"], lines, data["language"], data["audio_sha256"], {"kind": kind},
                   {"aligner": data["model"], "device": data["device"]["device"], "source": data.get("source", "mix")})
    return add_line_pauses(al)


def apply_result(al: Alignment, data: dict) -> Alignment:
    """Devuelve una copia con las líneas alineadas. Conserva ids, agente y referencias al TTML (línea y sílaba); recalcula pausas entre
    líneas y el desfase global (mediana de cambios por línea) y, si éste no es fiable, no lo aplica. Las voces de fondo no se alinearon:
    se mueven solo por el desfase global y quedan marcadas `approximate` con confianza 0, para revisión."""
    out, by_id, pairs = copy.deepcopy(al), {l["id"]: l for l in data["lines"]}, []
    for i, old in enumerate(out.lines):
        res = by_id.get(old.id)
        if old.bg or res is None:
            continue
        new = remap_line(old, res)
        pairs.append((old.start, new.start))
        out.lines[i] = new
    est = estimate_offset(pairs)
    if est["confidence"] < MIN_OFFSET_CONFIDENCE:
        est = {**est, "ms": 0, "method": "median_delta_rejected"}
    out.global_offset = est
    for line in out.lines:
        if line.bg and line.id not in by_id:  # voz de fondo sin alinear: solo el desfase global, y a revisión
            for u in (line, *line.words, *(s for w in line.words for s in w.syllables)):
                u.start, u.end = u.start + est["ms"], u.end + est["ms"]
                u.confidence, u.approximate = 0.0, True
    out.language, out.audio_sha256, out.duration_ms = data["language"], data["audio_sha256"], data["duration_ms"]
    out.pipeline = {"aligner": data["model"], "device": data["device"]["device"], "source": data.get("source", "mix")}
    return add_line_pauses(out)


def needs_review(al: Alignment, below: float = REVIEW_BELOW) -> list[str]:
    """ids de las palabras (o sílabas, si la palabra no tiene) a revisar: confianza baja, tiempo aproximado o de largo cero."""
    out = []
    for line in al.lines:
        for w in line.words:
            for u in (w.syllables or [w]):
                if u.manual_override:  # lo fijó una persona: ya está revisado
                    continue
                if u.confidence < below or u.approximate or u.end <= u.start:
                    out.append(u.id or f"{line.id}:{u.text}")
    return out
