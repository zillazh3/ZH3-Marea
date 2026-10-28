"""Modelo de letras (tiempos en ms) y parsers: TTML estilo Apple Music y LRC."""
import re
from dataclasses import dataclass, field

import yaml
import defusedxml.ElementTree as ET  # los .ttml vienen de fuera: sin entidades externas ni billion-laughs
from defusedxml import DefusedXmlException

INTERLUDE_MS = 2000
INTRO_MS = 1200  # la intro muestra los puntos desde aquí (como Apple Music); los huecos entre versos, desde INTERLUDE_MS

TT = "{http://www.w3.org/ns/ttml}"
TTM = "{http://www.w3.org/ns/ttml#metadata}"


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


@dataclass
class Line:
    start: int
    end: int  # incluye las voces de fondo
    agent: str = "v1"  # v2 = dueto
    syllables: list[Syllable] = field(default_factory=list)
    bg: list[BgVocal] = field(default_factory=list)
    approx: bool = False  # tiempos por sílaba repartidos a partir de una línea

    @property
    def text(self) -> str:
        return join_text(self.syllables)


@dataclass
class Interlude:
    start: int
    end: int


@dataclass
class Lyrics:
    lines: list[Line]
    source: str = ""  # de dónde salió: "apple_music", "spicy_lyrics", "lrclib"… (vacío = archivo propio)
    credit: str = ""  # quién la proporciona, para «Provided by …» («Apple Music»)
    writers: list[str] = field(default_factory=list)  # autores, para «Written by …»

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


def _timed_spans(parent, bg: bool = False) -> list[Syllable]:
    out: list[Syllable] = []
    for c in parent:
        if c.tag != f"{TT}span" or _role(c) or c.get("begin") is None:
            continue
        raw = "".join(c.itertext())
        if raw[:1].isspace() and out:
            out[-1].word_end = True
        tail = c.tail or ""
        out.append(Syllable(raw.strip(), parse_time(c.get("begin")), parse_time(c.get("end")),
                            word_end=raw[-1:].isspace() or tail.isspace()))
    if bg and out:  # Apple envuelve la voz de fondo en "( … )"
        out[0].text = out[0].text.lstrip("(")
        out[-1].text = out[-1].text.rstrip(")")
    out = [s for s in out if s.text]
    if out:
        out[-1].word_end = False
    return out


def _plain_text(p) -> str:
    """Texto de un <p> sin spans con tiempo, ignorando voces de fondo y traducciones."""
    parts = [p.text or ""]
    for c in p:
        if not _role(c):
            parts.append("".join(c.itertext()))
        parts.append(c.tail or "")
    return " ".join("".join(parts).split())


def _bg_vocal(span) -> BgVocal | None:
    syls = _timed_spans(span, bg=True)
    if not syls and span.get("begin") and span.get("end"):  # voz de fondo con tiempo solo por línea
        text = " ".join("".join(span.itertext()).strip("() \n").split())
        syls = spread(text, parse_time(span.get("begin")), parse_time(span.get("end")))
    if not syls:
        return None
    return BgVocal(parse_time(span.get("begin")) if span.get("begin") else syls[0].begin,
                   parse_time(span.get("end")) if span.get("end") else syls[-1].end, syls)


def _agent(raw: str | None, agents: dict) -> str:
    """v1/v2 se respetan; cualquier otro id (p. ej. 'person2') se asigna por orden de aparición."""
    if raw in ("v1", "v2") or raw is None:
        return raw or "v1"
    return agents.setdefault(raw, "v1" if not agents else "v2")


def _parse_p(p, agents: dict) -> Line | None:
    syls = _timed_spans(p)
    start = parse_time(p.get("begin")) if p.get("begin") else None
    end = parse_time(p.get("end")) if p.get("end") else None
    approx = False
    if not syls:  # TTML con tiempos solo por línea
        text = _plain_text(p)
        if text and start is not None and end is not None:
            syls, approx = spread(text, start, end), True
    if not syls:
        return None
    bg = [b for s in p if s.tag == f"{TT}span" and _role(s) == "x-bg" and (b := _bg_vocal(s))]
    start = syls[0].begin if start is None else start
    end = max([syls[-1].end if end is None else end] + [b.end for b in bg])
    return Line(start, end, _agent(p.get(f"{TTM}agent"), agents), syls, bg, approx)


def parse_ttml(source: str | bytes) -> Lyrics:
    if isinstance(source, str):
        source = source.encode("utf-8")
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(f"El archivo no es un TTML válido: {e}") from e
    if root.tag != f"{TT}tt":
        raise ValueError("El archivo no parece un TTML (falta la etiqueta <tt>).")
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
        raise ValueError("El lyricsfile es demasiado grande.")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"El lyricsfile no es un YAML válido: {e}") from e
    if not isinstance(doc, dict) or str(doc.get("version")) != "1.0":
        raise ValueError("Versión de lyricsfile no soportada (solo 1.0).")
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
                t, b = str(w["text"]), int(w["start_ms"])
                if t[:1].isspace() and syls:
                    syls[-1].word_end = True
                e = int(w["end_ms"]) if w.get("end_ms") is not None else (
                    int(words[j + 1]["start_ms"]) if j + 1 < len(words) else end)
                syls.append(Syllable(t.strip(), b, max(e, b), word_end=t[-1:].isspace()))
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
