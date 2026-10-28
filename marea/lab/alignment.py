"""alignment.v1.json: la fuente de verdad de la sincronía (tiempos en ms sobre el audio de referencia) y su validador.

Convenciones:
- `global_offset.ms` > 0 = el audio va retrasado respecto a la letra (igual que `audio_offset_ms` del proyecto). Los tiempos
  guardados NO lo incluyen: se aplica solo al exportar (`apply_offset`), de modo que el TTML queda en el reloj de la letra.
- `ref` enlaza cada unidad con el TTML de origen: líneas `{"ttml_p_index": i}` (+ `"bg_index": k` si es una voz de fondo
  `<span ttm:role="x-bg">`), sílabas `{"span_index": j}` = posición entre los `<span begin>` hijos directos sin rol de ese contenedor.
  Una voz de fondo sin TTML de origen lleva `{"parent": id de su línea principal}`.
"""
import copy
import json
import statistics
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SCHEMA, VERSION = "marea.alignment", 1


class AlignmentError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]) + (f" (+{len(errors) - 5} más)" if len(errors) > 5 else ""))
        self.errors = errors


@dataclass
class Unit:
    text: str
    start: int
    end: int
    confidence: float = 1.0
    approximate: bool = False  # el tiempo no se pudo medir con evidencia acústica suficiente
    manual_override: bool = False
    source: str = ""  # aligner que lo produjo: "ctc", "ttml", "manual"…
    id: str = ""
    ref: dict = field(default_factory=dict)


@dataclass
class Vowel(Unit):
    pass


@dataclass
class Syl(Unit):
    vowels: list[Vowel] = field(default_factory=list)


@dataclass
class Word(Unit):
    syllables: list[Syl] = field(default_factory=list)  # vacío = solo hay tiempo por palabra


@dataclass
class Pause:
    start: int
    end: int
    kind: str = "intra_line"  # "intra_line" | "between_lines"


@dataclass
class Line:
    start: int
    end: int
    words: list[Word] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    agent: str = "v1"
    bg: bool = False
    confidence: float = 1.0
    approximate: bool = False
    manual_override: bool = False
    source: str = ""
    id: str = ""
    ref: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def leaves(self) -> list[Unit]:
        """Lo que realmente se anima: sílabas, o la palabra si solo hay tiempo por palabra."""
        return [s for w in self.words for s in (w.syllables or [w])]


@dataclass
class Alignment:
    duration_ms: int
    lines: list[Line] = field(default_factory=list)
    language: str = ""
    audio_sha256: str = ""
    source_lyrics: dict = field(default_factory=dict)  # {"kind": "ttml|lrc|asr|text", "path_hint", "sha256"}
    pipeline: dict = field(default_factory=dict)
    global_offset: dict = field(default_factory=lambda: {"ms": 0, "confidence": 0.0, "method": "manual"})

    @property
    def offset_ms(self) -> int:
        return int(self.global_offset.get("ms", 0))


# ---------------- JSON ----------------

def to_dict(al: Alignment) -> dict:
    d = asdict(al)
    return {"schema": SCHEMA, "version": VERSION,
            "audio": {"sha256": d.pop("audio_sha256"), "duration_ms": d.pop("duration_ms")},
            "language": d.pop("language"), "source_lyrics": d.pop("source_lyrics"), "pipeline": d.pop("pipeline"),
            "global_offset": d.pop("global_offset"), "lines": d["lines"]}


def _build(cls, d: dict, **children):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names and k not in children}, **children)


def word_from_dict(w: dict) -> Word:
    return _build(Word, w, syllables=[_build(Syl, s, vowels=[_build(Vowel, v) for v in s.get("vowels", [])])
                                      for s in w.get("syllables", [])])


def line_from_dict(l: dict) -> Line:
    """También la usa el sidecar: lo que sale de `sidecar.pipeline.align_line` (+ id, agent, ref) es un dict de línea."""
    return _build(Line, l, words=[word_from_dict(w) for w in l.get("words", [])], pauses=[_build(Pause, p) for p in l.get("pauses", [])])


def from_dict(d: dict) -> Alignment:
    if d.get("schema") != SCHEMA or d.get("version") != VERSION:
        raise AlignmentError([f"Formato no soportado: {d.get('schema')!r} v{d.get('version')!r} (esta app lee {SCHEMA} v{VERSION})."])
    lines = [line_from_dict(l) for l in d.get("lines", [])]
    return Alignment(d["audio"]["duration_ms"], lines, d.get("language", ""), d["audio"].get("sha256", ""),
                     d.get("source_lyrics", {}), d.get("pipeline", {}),
                     d.get("global_offset") or {"ms": 0, "confidence": 0.0, "method": "manual"})


def save(al: Alignment, path: str | Path):
    Path(path).write_text(json.dumps(to_dict(al), ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: str | Path) -> Alignment:
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------- offset ----------------

def apply_offset(al: Alignment) -> Alignment:
    """Copia con el desfase global aplicado (tiempo de la letra = tiempo del audio − offset) y offset 0."""
    out, off = copy.deepcopy(al), al.offset_ms
    for line in out.lines:
        for u in (line, *line.pauses, *(x for w in line.words for x in (w, *w.syllables, *(v for s in w.syllables for v in s.vowels)))):
            u.start, u.end = u.start - off, u.end - off
    out.global_offset = {**al.global_offset, "ms": 0}
    return out


# ---------------- validador ----------------

def validate(al: Alignment) -> list[str]:
    """Lista de problemas (vacía = válido). Se ejecuta SIEMPRE antes de exportar."""
    errs: list[str] = []
    dur = al.duration_ms

    def span(label: str, u) -> bool:
        ok = u.start < u.end
        if not ok:
            errs.append(f"{label}: inicio {u.start} ≥ fin {u.end} ms")
        if u.start < 0 or u.end > dur:
            errs.append(f"{label}: fuera de la duración del audio (0–{dur} ms)")
        return ok

    def inside(label: str, u, parent):
        if u.start < parent.start or u.end > parent.end:
            errs.append(f"{label}: sale de su contenedor ({parent.start}–{parent.end} ms)")

    last_start: dict[tuple, int] = {}
    for line in al.lines:
        L = f"línea {line.id or line.text[:20]!r}"
        span(L, line)
        voice = (line.agent, line.bg)
        if line.start < last_start.get(voice, line.start):
            errs.append(f"{L}: empieza antes que la línea anterior de su voz (orden no monótono)")
        last_start[voice] = line.start
        prev_end = None
        for w in line.words:
            W = f"{L}, palabra {w.text!r}"
            span(W, w)
            inside(W, w, line)
            if w.syllables and "".join(s.text for s in w.syllables) != w.text:
                errs.append(f"{W}: sus sílabas no reconstruyen el texto")
            for s in w.syllables:
                S = f"{W}, sílaba {s.text!r}"
                if not s.text.strip():
                    errs.append(f"{S}: texto vacío (una pausa no puede ser una unidad activa)")
                span(S, s)
                inside(S, s, w)
                pv = None
                for v in s.vowels:
                    span(f"{S}, vocal {v.text!r}", v)
                    inside(f"{S}, vocal {v.text!r}", v, s)
                    if pv is not None and v.start < pv:
                        errs.append(f"{S}: vocales fuera de orden")
                    pv = v.end
            for u in (w.syllables or [w]):
                if prev_end is not None and u.start < prev_end:
                    errs.append(f"{L}, {u.text!r}: se solapa con la unidad anterior ({u.start} < {prev_end} ms)")
                prev_end = u.end
        for p in line.pauses:
            span(f"{L}, pausa", p)
            hit = next((u for u in line.leaves if u.start < p.end and p.start < u.end), None)
            if hit:
                errs.append(f"{L}: una pausa ({p.start}–{p.end} ms) se solapa con texto activo {hit.text!r}")
    return errs


# ---------------- recálculo parcial ----------------

def _manual_units(lines: list[Line]) -> dict[str, Unit]:
    return {u.id: u for l in lines for w in l.words for u in (w, *w.syllables) if u.manual_override and u.id}


def merge(al: Alignment, old_ids: list[str], new_lines: list[Line]) -> Alignment:
    """Reemplaza las líneas `old_ids` por `new_lines` (recalculadas, o varias si el usuario partió una) sin tocar el resto.

    Lo que el usuario fijó a mano (`manual_override`) se conserva con su subárbol si la unidad nueva tiene el mismo `id`: una línea
    fijada se queda entera, una palabra fijada conserva sus sílabas, una sílaba fijada conserva sus vocales. Si el resultado
    introduce errores que antes no había (p. ej. se solapa con una vecina) levanta AlignmentError y no devuelve nada."""
    have = {l.id for l in al.lines}
    missing = [i for i in old_ids if i not in have]
    if missing or not old_ids:
        raise AlignmentError([f"líneas que no existen en la alineación: {missing}" if missing else "no se indicó qué líneas reemplazar"])
    old = [l for l in al.lines if l.id in old_ids]
    pinned_lines = {l.id: l for l in old if l.manual_override}
    pinned = _manual_units(old)
    fresh = []
    for line in copy.deepcopy(new_lines):
        if line.id in pinned_lines:
            line = copy.deepcopy(pinned_lines[line.id])
        else:
            line.words = [copy.deepcopy(pinned[w.id]) if w.id in pinned and isinstance(pinned[w.id], Word) else w for w in line.words]
            for w in line.words:
                w.syllables = [copy.deepcopy(pinned[s.id]) if s.id in pinned and isinstance(pinned[s.id], Syl) else s for s in w.syllables]
        for w in line.words:  # lo fijado a mano puede salirse de lo recalculado: el contenedor crece, nunca se encoge
            for s in w.syllables:
                w.start, w.end = min(w.start, s.start), max(w.end, s.end)
            line.start, line.end = min(line.start, w.start), max(line.end, w.end)
        fresh.append(line)
    out = copy.deepcopy(al)
    at = next(i for i, l in enumerate(out.lines) if l.id in old_ids)
    out.lines = [l for l in out.lines if l.id not in old_ids]
    out.lines[at:at] = fresh
    before = set(validate(al))
    introduced = [e for e in validate(out) if e not in before]
    if introduced:
        raise AlignmentError(introduced)
    return out


# ---------------- offset global ----------------

MIN_PAIRS = 5  # con menos líneas que esto la mediana no dice nada


def estimate_offset(pairs: list[tuple[int, int]]) -> dict:
    """Desfase global a partir de [(inicio de línea en la letra de referencia, inicio alineado en el audio)] en ms.

    ms = mediana de las diferencias (>0 = el audio va retrasado respecto a la letra). confidence = fracción de líneas a ≤ 100 ms de
    esa mediana (tolerancia absoluta: una tolerancia relativa a la dispersión daría por bueno cualquier desorden). Con pocas
    líneas o confianza baja no hay un desfase global y se pide calibración manual. Es una segunda opinión sobre
    `audio.align.detect_offset`, que mira solo el audio."""
    deltas = [b - a for a, b in pairs]
    if len(deltas) < MIN_PAIRS:
        return {"ms": 0, "confidence": 0.0, "method": "median_delta"}
    med = statistics.median(deltas)
    inliers = sum(abs(d - med) <= 100 for d in deltas)
    return {"ms": round(med), "confidence": round(inliers / len(deltas), 3), "method": "median_delta"}


# ---------------- pausas entre líneas ----------------

def add_line_pauses(al: Alignment, min_ms: int = 150) -> Alignment:
    """Marca como pausa (`between_lines`, colgada de la línea que termina) el hueco hasta la siguiente línea de la misma voz.
    Si el fin de esa línea no está medido (`approximate`), el hueco tampoco: no se marca."""
    out = copy.deepcopy(al)
    for voice in {(l.agent, l.bg) for l in out.lines}:
        lines = [l for l in out.lines if (l.agent, l.bg) == voice]
        for a, b in zip(lines, lines[1:]):
            a.pauses = [p for p in a.pauses if p.kind != "between_lines"]
            last = a.leaves[-1] if a.leaves else None
            if last is not None and not last.approximate and b.start - a.end >= min_ms:
                a.pauses.append(Pause(a.end, b.start, "between_lines"))
    return out
