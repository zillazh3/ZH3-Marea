"""Modelo de letras (tiempos en ms) y parsers: TTML estilo Apple Music y LRC."""
import re
from dataclasses import dataclass, field

import yaml
import defusedxml.ElementTree as ET  # los .ttml vienen de fuera: sin entidades externas ni billion-laughs
from defusedxml import DefusedXmlException

from marea.i18n import t

INTERLUDE_MS = 2000
INTRO_MS = 1200  # la intro muestra los puntos desde aquí (como Apple Music); los huecos entre versos, desde INTERLUDE_MS

TT = "{http://www.w3.org/ns/ttml}"
TTM = "{http://www.w3.org/ns/ttml#metadata}"
XML = "{http://www.w3.org/XML/1998/namespace}"
MAREA_URI = "https://zh3.marea/ns/ttml"
MAREA = f"{{{MAREA_URI}}}"


@dataclass
class Syllable:
    text: str
    begin: int
    end: int
    word_end: bool = False  # True si después de la sílaba va un espacio


@dataclass
class BgVocal:
    start: int
    end: int
    syllables: list[Syllable] = field(default_factory=list)
    role: str = "backing"
    agent: str = ""


@dataclass
class Line:
    start: int
    end: int  # incluye las voces de fondo
    agent: str = "v1"  # v2 = dueto
    syllables: list[Syllable] = field(default_factory=list)
    bg: list[BgVocal] = field(default_factory=list)
    approx: bool = False  # tiempos por sílaba repartidos a partir de una línea
    role: str = ""
    raw_agent: str = ""

    @property
    def text(self) -> str:
        return join_text(self.syllables)


@dataclass
class Interlude:
    start: int
    end: int


SOURCE_SPICY = "spicy_lyrics"
SOURCE_LRCLIB = "lrclib"
SOURCE_MANUAL = "manual"
SOURCE_UNKNOWN = "unknown"
VALID_SOURCE_TAGS = (SOURCE_SPICY, SOURCE_LRCLIB, SOURCE_MANUAL, SOURCE_UNKNOWN)
_SPICY_ALIASES = frozenset({"spicy_lyrics", "apple_music", "spotify"})


def resolve_source_tag(
    source: str = "",
    provider_tag: str = "",
    *,
    manual_override: bool = False,
    has_local_file: bool = False,
) -> str:
    """Resolve the canonical lyrics source tag from stored fields and fallback hints."""
    if manual_override:
        return SOURCE_MANUAL
    tag = (provider_tag or "").strip().lower()
    if tag in VALID_SOURCE_TAGS:
        return tag
    src = (source or "").strip().lower()
    if src in _SPICY_ALIASES:
        return SOURCE_SPICY
    if src == SOURCE_LRCLIB:
        return SOURCE_LRCLIB
    if src == SOURCE_MANUAL or (not src and has_local_file):
        return SOURCE_MANUAL
    return SOURCE_UNKNOWN


@dataclass
class Lyrics:
    lines: list[Line]
    source: str = ""  # de dónde salió: "apple_music", "spicy_lyrics", "lrclib"… (vacío = archivo propio)
    credit: str = ""  # quién la proporciona, para «Provided by …» («Apple Music»)
    writers: list[str] = field(default_factory=list)  # autores, para «Written by …»
    provider_tag: str = ""  # canonical tag: "spicy_lyrics" | "lrclib" | "manual" | "unknown"
    manual_override: bool = False

    @property
    def resolved_source_tag(self) -> str:
        return resolve_source_tag(self.source, self.provider_tag, manual_override=self.manual_override)

    def credit_lines(self) -> list[tuple[str, str]]:
        """[(prefijo, texto)] que se muestra bajo la letra, como en Spicetify: «Written by A, B» y «Provided by Apple Music»."""
        return [(label, text) for label, text in (("Written by", ", ".join(self.writers)), ("Provided by", self.credit)) if text]

    @property
    def kind(self) -> str:
        """'syllable' si los tiempos vienen por sílaba/palabra; 'line' si alguna línea se repartió (approx)."""
        return "line" if any(l.approx for l in self.lines) else "syllable"

    @property
    def interludes(self) -> list[Interlude]:
        """Huecos ≥ 2 s entre líneas; la intro cuenta desde 1.2 s (siempre hay puntos antes de empezar a cantar)."""
        out, prev_end = [], 0
        for i, line in enumerate(self.lines):
            if line.start - prev_end >= (INTRO_MS if i == 0 else INTERLUDE_MS):
                out.append(Interlude(prev_end, line.start))
            prev_end = max(prev_end, line.end)
        return out


def join_text(syllables: list[Syllable]) -> str:
    return "".join(s.text + (" " if s.word_end else "") for s in syllables).strip()


def parse_time(s: str) -> int:
    """'00:06.000', '1:02.5', '6.5s', '500ms' -> ms."""
    s = s.strip()
    if s.endswith("ms"):
        return round(float(s[:-2]))
    if s.endswith("s"):
        return round(float(s[:-1]) * 1000)
    sec = 0.0
    for part in s.split(":"):
        sec = sec * 60 + float(part)
    return round(sec * 1000)


def spread(text: str, start: int, end: int) -> list[Syllable]:
    """Reparte [start, end] entre las palabras según su número de letras (una sílaba por palabra)."""
    words = text.split()
    weights = [max(1, sum(c.isalnum() for c in w)) for w in words]
    total, acc, t0, out = sum(weights), 0, start, []
    for i, (w, k) in enumerate(zip(words, weights)):
        acc += k
        t1 = start + round((end - start) * acc / total)
        out.append(Syllable(w, t0, t1, word_end=i < len(words) - 1))
        t0 = t1
    return out


# ---------------- TTML ----------------

def _role(el) -> str | None:
    return el.get(f"{TTM}role")


def timed_span_children(container) -> list:
    """Return direct child `<span>` elements carrying a `begin` attribute and no `ttm:role`."""
    return [c for c in container if c.tag == f"{TT}span" and not _role(c) and c.get("begin") is not None]


def _span_end_ms(spans: list, idx: int, begin_ms: int, fallback_end: int | None) -> int:
    raw_end = spans[idx].get("end")
    if raw_end is not None:
        return parse_time(raw_end)
    if idx + 1 < len(spans):
        return max(begin_ms, parse_time(spans[idx + 1].get("begin")))
    return max(begin_ms, fallback_end if fallback_end is not None else begin_ms)


def iter_word_groups(container, bg: bool = False, fallback_end: int | None = None) -> list[list[tuple[int, str, int, int]]]:
    """Group timed child spans into words using TTML whitespace rules: leading/trailing or tail whitespace delimits words.

    Returns lists of `(span_index, text, begin_ms, end_ms)` tuples.
    """
    spans = timed_span_children(container)
    groups: list[list[tuple[int, str, int, int]]] = [[]]
    for j, c in enumerate(spans):
        raw = "".join(c.itertext())
        if raw[:1].isspace() and groups[-1]:
            groups.append([])
        b = parse_time(c.get("begin"))
        e = _span_end_ms(spans, j, b, fallback_end)
        text = raw.strip()
        if text:
            groups[-1].append((j, text, b, e))
        if raw[-1:].isspace() or (c.tail or "").isspace():
            groups.append([])
    groups = [g for g in groups if g]
    if bg and groups:  # Apple wraps background vocals in "( ... )"
        idx0, t0, b0, e0 = groups[0][0]
        groups[0][0] = (idx0, t0.lstrip("("), b0, e0)
        idx1, t1, b1, e1 = groups[-1][-1]
        groups[-1][-1] = (idx1, t1.rstrip(")"), b1, e1)
        groups = [[item for item in g if item[1]] for g in groups]
        groups = [g for g in groups if g]
    return groups


def _append_word_syllable(out: list[Syllable], raw: str, b: int, e: int, tail_space: bool = False):
    if raw[:1].isspace() and out:
        out[-1].word_end = True
    text = raw.strip()
    if text:
        out.append(Syllable(text, b, max(e, b), word_end=raw[-1:].isspace() or tail_space))


def _timed_spans(parent, bg: bool = False, fallback_end: int | None = None) -> list[Syllable]:
    groups = iter_word_groups(parent, bg=bg, fallback_end=fallback_end)
    out: list[Syllable] = []
    for g in groups:
        for _, text, b, e in g:
            out.append(Syllable(text, b, e, word_end=False))
        out[-1].word_end = True
    if out:
        out[-1].word_end = False
    return out


def plain_text(p) -> str:
    """Texto de un <p> sin spans con tiempo, ignorando voces de fondo y traducciones."""
    parts = [p.text or ""]
    for c in p:
        if not _role(c):
            parts.append("".join(c.itertext()))
        parts.append(c.tail or "")
    return " ".join("".join(parts).split())


_plain_text = plain_text


def _bg_vocal(span) -> BgVocal | None:
    syls = _timed_spans(span, bg=True)
    if not syls and span.get("begin") and span.get("end"):  # voz de fondo con tiempo solo por línea
        text = " ".join("".join(span.itertext()).strip("() \n").split())
        if text:
            syls = spread(text, parse_time(span.get("begin")), parse_time(span.get("end")))
    if not syls:
        return None
    role = span.get(f"{MAREA}role") or "backing"
    agent = span.get(f"{TTM}agent") or ""
    return BgVocal(parse_time(span.get("begin")) if span.get("begin") else syls[0].begin,
                   parse_time(span.get("end")) if span.get("end") else syls[-1].end, syls, role=role, agent=agent)


def _agent(raw: str | None, agents: dict) -> str:
    """v1/v2 se respetan; cualquier otro id (p. ej. 'person2') se asigna por orden de aparición."""
    if raw in ("v1", "v2") or raw is None:
        return raw or "v1"
    return agents.setdefault(raw, "v1" if not agents else "v2")


def _parse_p(p, agents: dict) -> Line | None:
    start = parse_time(p.get("begin")) if p.get("begin") else None
    end = parse_time(p.get("end")) if p.get("end") else None
    syls = _timed_spans(p, fallback_end=end)
    approx = False
    if not syls:  # TTML con tiempos solo por línea
        text = plain_text(p)
        if text and start is not None and end is not None:
            syls, approx = spread(text, start, end), True
    bg = [b for s in p if s.tag == f"{TT}span" and _role(s) == "x-bg" and (b := _bg_vocal(s))]
    if not syls and not bg:
        return None
    raw_ag = p.get(f"{TTM}agent") or (bg[0].agent if not syls and bg else "") or ""
    mrole = p.get(f"{MAREA}role") or (bg[0].role if not syls and bg else "")
    if syls:
        start = min([syls[0].begin if start is None else start] + [b.start for b in bg])
        end = max([syls[-1].end if end is None else end] + [b.end for b in bg])
    else:
        start = min([b.start for b in bg] + ([start] if start is not None else []))
        end = max([b.end for b in bg] + ([end] if end is not None else []))
    return Line(start, end, _agent(raw_ag or None, agents), syls, bg, approx, role=mrole, raw_agent=raw_ag)


def parse_ttml(source: str | bytes) -> Lyrics:
    if isinstance(source, str):
        source = source.encode("utf-8")
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(t("errors.lyrics.ttmlInvalid", error=e)) from e
    if root.tag != f"{TT}tt":
        raise ValueError(t("errors.lyrics.ttmlNoTt"))
    agents: dict = {}
    lines = [line for p in root.iter(f"{TT}p") if (line := _parse_p(p, agents))]
    return Lyrics(sorted(lines, key=lambda l: l.start))


# ---------------- Lyricsfile (formato de LRCLIB, YAML) ----------------

# Sin fin explícito, un verso dura hasta el siguiente; solo si el hueco es enorme se asume que cantó menos y hay interludio.
# Un tope bajo (p. ej. 100 ms/letra) creaba interludios falsos entre versos lentos consecutivos.
LRC_MIN_MS, LRC_MS_PER_CHAR = 5000, 150


def _guess_end(start: int, text: str, next_start: int | None) -> int:
    # ponytail: tope de 5 s o 150 ms/letra; ajustar si faltan o sobran interludios
    cap = start + max(LRC_MIN_MS, LRC_MS_PER_CHAR * len(text))
    return min(next_start, cap) if next_start is not None else cap


def parse_lyricsfile(text: str) -> Lyrics:
    """Lyricsfile 1.0 (borrador de tranxuanthang/lyricsfile): líneas con `words` opcional por palabra."""
    if len(text) > 1_000_000:  # ponytail: solo tope de tamaño; el borrador desaconseja anclas/alias, no se rechazan
        raise ValueError(t("errors.lyrics.lyricsfileTooBig"))
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(t("errors.lyrics.lyricsfileYaml", error=e)) from e
    if not isinstance(doc, dict) or str(doc.get("version")) != "1.0":
        raise ValueError(t("errors.lyrics.lyricsfileVersion"))
    raw = sorted((l for l in doc.get("lines") or [] if isinstance(l, dict) and str(l.get("text", "")).strip()
                  and l.get("start_ms") is not None), key=lambda l: l["start_ms"])
    lines = []
    for i, l in enumerate(raw):
        start = int(l["start_ms"])
        nxt = int(raw[i + 1]["start_ms"]) if i + 1 < len(raw) else None
        end = max(int(l["end_ms"]) if l.get("end_ms") is not None else _guess_end(start, str(l["text"]), nxt), start + 1)
        words = [w for w in l.get("words") or [] if isinstance(w, dict) and str(w.get("text", "")).strip()
                 and w.get("start_ms") is not None]
        approx = not words
        if words:
            syls: list[Syllable] = []
            for j, w in enumerate(words):
                txt, b = str(w["text"]), int(w["start_ms"])
                e = int(w["end_ms"]) if w.get("end_ms") is not None else (
                    int(words[j + 1]["start_ms"]) if j + 1 < len(words) else end)
                _append_word_syllable(syls, txt, b, e)
            if syls:
                syls[-1].word_end = False
        else:
            syls = spread(str(l["text"]), start, end)
        lines.append(Line(start, max(end, syls[-1].end), syllables=syls, approx=approx))
    return Lyrics(lines)


# ---------------- LRC ----------------

_LRC_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_LRC_WORD = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")  # LRC "enhanced": tiempo de cada palabra o sílaba


def _enhanced_syllables(body: str, line_end: int) -> list[Syllable]:
    """'<00:12.00>Ho<00:12.30>la <00:12.80>mundo<00:13.40>' -> sílabas con tiempo real. Una etiqueta sin texto marca el fin de la anterior."""
    parts = _LRC_WORD.split(body)  # [texto previo, min, seg, texto, min, seg, texto, ...]
    tagged = [(round((int(m) * 60 + float(s)) * 1000), txt) for m, s, txt in zip(parts[1::3], parts[2::3], parts[3::3])]
    out = []
    for i, (t, txt) in enumerate(tagged):
        if txt.strip():
            end = tagged[i + 1][0] if i + 1 < len(tagged) else line_end
            out.append(Syllable(txt.strip(), t, max(end, t + 1), word_end=txt[-1:].isspace()))
    if out:
        out[-1].word_end = False
    return out


def parse_lrc(text: str) -> Lyrics:
    """LRC -> Lyrics. Por línea (`approx=True`) o, si trae tiempos por palabra (`<mm:ss.xx>`), con sílabas reales.
    Una línea vacía actúa como marca de fin de la anterior."""
    entries = []
    for raw in text.splitlines():
        stamps = _LRC_STAMP.findall(raw)
        rest = _LRC_STAMP.sub("", raw)
        body = _LRC_WORD.sub("", rest).strip()
        entries += [(round((int(m) * 60 + float(s)) * 1000), body, rest) for m, s in stamps]
    entries.sort(key=lambda e: e[0])
    lines = []
    for i, (t, body, rest) in enumerate(entries):
        if not body:
            continue
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        end = nxt[0] if nxt and not nxt[1] else _guess_end(t, body, nxt[0] if nxt else None)
        end = max(end, t + 1)
        syls = _enhanced_syllables(rest, end) if _LRC_WORD.search(rest) else []
        if syls:  # tiempos reales: no es una letra «por línea»
            closed = not _LRC_WORD.split(rest)[-1].strip()  # termina en una etiqueta sin texto: el fin lo dice la letra, no una suposición
            lines.append(Line(min(t, syls[0].begin), syls[-1].end if closed else max(end, syls[-1].end), syllables=syls))
        else:
            lines.append(Line(t, end, syllables=spread(body, t, end), approx=True))
    return Lyrics(lines)
