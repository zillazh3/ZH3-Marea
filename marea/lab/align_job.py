"""De un Alignment (Modo A: `from_ttml`) o un borrador (Modo B: `asr`/texto) a la petición `align` del sidecar y de vuelta."""
import copy
import re
from dataclasses import asdict

from marea.lab.alignment import BG_ROLES, ROLES, Alignment, Line, add_line_pauses, assign_layers, estimate_offset, line_from_dict

MIN_OFFSET_CONFIDENCE = 0.7  # ponytail: provisional; por debajo no se aplica ningún desfase global (se pide calibración manual)
REVIEW_BELOW = 0.1  # ponytail: calibrado con 7 canciones de JamendoLyrics (2131 palabras): marca ~42 % y cubre ~90 % de las palabras con error > 500 ms
ROLE_REVIEW_BELOW = 0.6  # capas con rol incierto o poca certeza acústica pasan a revisión humana

_TAG_RE = re.compile(r"^\[([a-zA-Z0-9_?:-]+)\]\s*(.+)$")
_ROLE_ALIASES = {
    "lead": "lead", "principal": "lead",
    "bg": "backing", "backing": "backing", "coro": "backing",
    "harmony": "harmony", "armonia": "harmony",
    "adlib": "adlib", "ad-lib": "adlib",
    "duet": "duet", "dueto": "duet",
    "?": "uncertain", "uncertain": "uncertain", "incierto": "uncertain",
}


def request_lines(al: Alignment) -> list[dict]:
    """Líneas principales con texto, en el formato clásico del sidecar: [{"id", "words": [[sílabas]]}]."""
    return [{"id": l.id, "words": [[s.text for s in w.syllables] or [w.text] for w in l.words]}
            for l in al.lines if not l.bg and l.words]


def request_layers(al: Alignment) -> list[dict]:
    """Todas las capas vocales (principal, duetos, coros, armonías, ad-libs…) con sus pistas de ventana para alineación independiente."""
    out = []
    for l in al.lines:
        if not l.words:
            continue
        d = {
            "id": l.id,
            "words": [[s.text for s in w.syllables] or [w.text] for w in l.words],
            "agent": l.agent,
            "bg": l.bg,
            "role": l.role,
            "layer": l.layer,
            "hint": [l.start, l.end],
            "source_stem": l.source_stem,
            "review_state": l.review_state,
            "role_confidence": l.role_confidence,
            "fallback": asdict(l),
        }
        if l.ref.get("parent"):
            d["parent"] = l.ref["parent"]
        if not any(w.syllables for w in l.words) and any(" " in w.text for w in l.words):
            d.pop("words")
            d["text"] = l.text
        out.append(d)
    return out


def remap_line(old: Line, res: dict) -> Line:
    """La línea alineada `res` (dict del sidecar) con la identidad de `old`: id, agente, rol, capa, referencia al TTML y los ids/referencias de
    cada palabra y sílaba (el sidecar no los conoce). Si el número de sílabas cambió, las sílabas nuevas quedan sin referencia."""
    new = line_from_dict({
        **res,
        "id": old.id,
        "agent": res.get("agent", old.agent),
        "bg": res.get("bg", old.bg),
        "role": res.get("role", old.role),
        "role_confidence": res.get("role_confidence", old.role_confidence),
        "layer": res.get("layer", old.layer),
        "ref": old.ref,
        "source": "ctc",
        "source_stem": res.get("source_stem") or old.source_stem,
        "review_state": old.review_state,
        "role_reviewed": old.role_reviewed,
    })
    old_s, new_s = [s for w in old.words for s in w.syllables], [s for w in new.words for s in w.syllables]
    if old_s and len(old_s) == len(new_s):
        for o, n in zip(old_s, new_s):
            n.id, n.ref = o.id, o.ref
    for o, n in zip(old.words, new.words):
        n.id = o.id
    ws = [w.confidence for w in new.words]
    new.confidence, new.approximate = (sum(ws) / len(ws) if ws else 0.0), any(w.approximate for w in new.words)
    return new


def format_draft_line(entry: dict) -> str:
    """Formatea una línea de `asr` para el cuadro de borrador sin mezclar coros en la letra principal."""
    text = str(entry.get("text", "")).strip()
    role = entry.get("role", "") or ("backing" if entry.get("bg") else "lead")
    agent = entry.get("agent", "v1")
    if role == "lead" and agent in ("", "v1") and not entry.get("bg"):
        return text
    if role == "duet":
        return f"[{agent}] {text}" if agent != "v1" else f"[duet] {text}"
    tag = f"{role}:{agent}" if agent and agent != "v1" else role
    return f"[{tag}] {text}"


def _parse_tag(raw: str) -> tuple[str, dict]:
    m = _TAG_RE.match(raw.strip())
    if not m:
        return raw.strip(), {}
    spec, text = m.group(1).strip(), m.group(2).strip()
    parts = spec.split(":", 1)
    head = parts[0].lower()
    if head in _ROLE_ALIASES:
        role = _ROLE_ALIASES[head]
        agent = parts[1] if len(parts) == 2 else ""
        bg = role in BG_ROLES or role == "uncertain"
        return text, {"role": role, "agent": agent, "bg": bg}
    if re.fullmatch(r"v\d+|person\d+", head):
        role = _ROLE_ALIASES.get(parts[1].lower(), "duet" if head != "v1" else "lead") if len(parts) == 2 else ("duet" if head != "v1" else "lead")
        return text, {"role": role, "agent": head, "bg": role in BG_ROLES}
    return raw.strip(), {}


def text_request_lines(texts: list[str], draft: list[dict] | None = None) -> list[dict]:
    """Modo B: el texto (ya corregido) línea por línea; reconoce etiquetas `[backing]`, `[harmony]`, `[adlib]`, `[v2]`, `[uncertain]`
    para mantener cada capa vocal independiente sin alterar el formato simple `[{"id", "text"}]` de líneas principales."""
    pool = list(draft or [])
    used: set[int] = set()
    out = []
    for i, raw in enumerate(texts):
        if not raw.strip():
            continue
        text, meta = _parse_tag(raw)
        if not text:
            continue
        entry = {"id": f"L{i}", "text": text, **meta}
        match = next((k for k, d in enumerate(pool) if k not in used and str(d.get("text", "")).strip() == text
                      and (not meta or d.get("role", "lead") == meta.get("role"))
                      and (":" not in raw.split("]", 1)[0] or d.get("agent", "v1") == meta.get("agent"))), None)
        # Keep identity when a user corrects a draft in place; reordered exact matches take precedence.
        if match is None and i < len(pool) and i not in used and len(texts) == len(pool):
            match = i
        if match is not None:
            d = pool[match]
            used.add(match)
            entry.update({key: d[key] for key in ("role", "agent", "bg", "source_stem", "review_state", "role_confidence", "layer") if key in d})
            entry.update({key: value for key, value in meta.items() if key != "agent" or ":" in raw.split("]", 1)[0]})
            if "start" in d and "end" in d:
                entry["hint"] = [int(d["start"]), int(d["end"])]
        out.append(entry)
    if any(d.get("review_state") == "pending" for k, d in enumerate(pool) if k not in used):
        raise ValueError("Keep pending candidate lines in the draft; accept or reject them in review after alignment.")
    return out


def link_secondary_lines(lines: list[Line]) -> list[Line]:
    """Enlaza voces secundarias (`bg=True`) sin TTML de origen a la línea principal con mayor solape, o las marca `standalone=True`."""
    mains = [l for l in lines if not l.bg and l.id]
    main_ids = {l.id for l in mains}
    for line in lines:
        if not line.bg:
            continue
        if line.ref.get("parent") in main_ids or line.ref.get("standalone") or "ttml_p_index" in line.ref:
            continue
        best, best_ov = None, 0
        for m in mains:
            ov = min(line.end, m.end) - max(line.start, m.start)
            if ov > best_ov:
                best, best_ov = m, ov
        line.ref = {k: v for k, v in line.ref.items() if k not in ("parent", "standalone")}
        if best is not None:
            line.ref["parent"] = best.id
        else:
            line.ref["standalone"] = True
    assign_layers(lines)
    return lines


def from_text_result(data: dict, kind: str = "text") -> Alignment:
    """Modo B: Alignment nuevo a partir del resultado del sidecar (sin TTML de origen: enlaza capas secundarias independientes)."""
    lines = []
    for r in data["lines"]:
        ref = {}
        if r.get("parent"):
            ref["parent"] = r["parent"]
        if r.get("standalone"):
            ref["standalone"] = True
        clean = {k: v for k, v in r.items() if k not in ("parent", "standalone")}
        line = line_from_dict({**clean, "ref": ref, "source": "ctc"})
        for n, w in enumerate(line.words):
            w.id = f"{line.id}.W{n}"
            for k, s in enumerate(w.syllables):
                s.id = f"{w.id}.S{k}"
        ws = [w.confidence for w in line.words]
        line.confidence, line.approximate = (sum(ws) / len(ws) if ws else 0.0), any(w.approximate for w in line.words)
        lines.append(line)
    link_secondary_lines(lines)
    al = Alignment(data["duration_ms"], lines, data["language"], data["audio_sha256"], {"kind": kind},
                   {"aligner": data["model"], "device": data["device"]["device"], "source": data.get("source", "mix"),
                    "separate": data.get("separate", ""), "karaoke": data.get("karaoke", "")})
    return add_line_pauses(al)


def apply_result(al: Alignment, data: dict) -> Alignment:
    """Devuelve una copia con todas las capas alineadas independientemente. Conserva ids, agente, rol y referencias al TTML; recalcula
    pausas entre líneas por capa y el desfase global sobre las líneas principales. Si una voz de fondo no vino en `data` (p. ej. un
    sidecar simulado de solo voz principal), cae al desplazamiento por desfase global y queda marcada `approximate` para revisión."""
    out, by_id, pairs = copy.deepcopy(al), {l["id"]: l for l in data["lines"]}, []
    for i, old in enumerate(out.lines):
        res = by_id.get(old.id)
        if res is None:
            continue
        new = remap_line(old, res)
        if not old.bg:
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
    assign_layers(out.lines)
    out.language, out.audio_sha256, out.duration_ms = data["language"], data["audio_sha256"], data["duration_ms"]
    out.pipeline = {"aligner": data["model"], "device": data["device"]["device"], "source": data.get("source", "mix"),
                    "separate": data.get("separate", ""), "karaoke": data.get("karaoke", "")}
    return add_line_pauses(out)


def needs_review(al: Alignment, below: float = REVIEW_BELOW) -> list[str]:
    """ids de líneas (si su clasificación de capa es incierta) y palabras/sílabas (confianza baja, tiempo aproximado o largo cero) a revisar."""
    out = []
    for line in al.lines:
        if line.review_state == "pending" or (not line.role_reviewed and not line.manual_override
                                              and (line.role == "uncertain" or line.role_confidence < ROLE_REVIEW_BELOW)):
            out.append(line.id or f"line:{line.text[:16]}")
        for w in line.words:
            for u in (w.syllables or [w]):
                if u.manual_override:  # lo fijó una persona: ya está revisado
                    continue
                if u.confidence < below or u.approximate or u.end <= u.start:
                    out.append(u.id or f"{line.id}:{u.text}")
    return out
