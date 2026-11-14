"""Reglas de contexto de las palabras con efecto (JSON editable): frases, negación, reversión, guardas de categoría gramatical y contraste.

Se carga la copia del usuario (`app_data()/wordfx/rules.json`) si existe; si no, la incluida en `assets/wordfx/`.
Solo datos: la lógica que las aplica vive en `matcher`. Los nombres de categoría NO se validan contra la biblioteca
(el matcher ignora los que no existan), así reglas y biblioteca evolucionan por separado. Todo se normaliza al cargar.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from marea.wordfx.lexicon import normalize
from marea.wordfx.library import LANGS

BUNDLED = Path(__file__).resolve().parent.parent.parent / "assets" / "wordfx" / "rules.json"
WILDCARD = "*"
SIDES = ("before", "after")
_TOP = {"phrases", "negators", "negation", "reversals", "pos_guards", "contrast", "senses"}


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class Phrase:
    tokens: tuple[str, ...]  # formas o lemas normalizados; «*» = exactamente una palabra cualquiera
    lang: str
    categories: tuple[str, ...]
    label: str  # patrón legible, para `Match.note`
    at: int = -1  # índice de la palabra del patrón que recibe el efecto (-1 = la última)


@dataclass(frozen=True)
class Cond:
    prev: frozenset[str] = frozenset()  # la palabra anterior (forma o lema) está en la lista
    next: frozenset[str] = frozenset()
    line_end: bool = False  # es la última palabra del verso
    line_start: bool = False


@dataclass(frozen=True)
class Guard:
    keep_if: Cond | None  # sin ninguna condición cierta, la palabra se descarta
    drop_if: Cond | None  # con alguna condición cierta, la palabra se descarta (gana a keep_if)


@dataclass(frozen=True)
class Reversal:
    trigger: tuple[str, ...]  # lemas consecutivos pegados a la palabra, por delante («volver», «a») o por detrás
    side: str
    lang: str
    map: dict[str, tuple[str, ...]]  # categoría -> categorías de recuperación


@dataclass(frozen=True)
class SenseCond:
    prev: frozenset[str] = frozenset()
    next: frozenset[str] = frozenset()
    near: frozenset[str] = frozenset()  # forma o lema en cualquier otro token del mismo verso
    co_cats: frozenset[str] = frozenset()  # categoría presente en otro token del mismo verso
    line_end: bool = False
    line_start: bool = False


@dataclass(frozen=True)
class SenseRule:
    word: str
    lang: str
    cond: SenseCond
    categories: tuple[str, ...]
    label: str = ""


@dataclass(frozen=True)
class Rules:
    phrases: tuple[Phrase, ...] = ()
    negators: dict[str, frozenset[str]] = field(default_factory=dict)  # lang -> palabras
    negation: dict[str, tuple[str, ...]] = field(default_factory=dict)  # categoría -> reemplazo (vacío = anula)
    reversals: tuple[Reversal, ...] = ()
    guards: dict[tuple[str, str], tuple[Guard, ...]] = field(default_factory=dict)  # (lang, palabra) -> guardas
    contrast_categories: frozenset[frozenset[str]] = frozenset()
    contrast_words: tuple[tuple[str, str], ...] = ()
    senses: dict[tuple[str, str], tuple[SenseRule, ...]] = field(default_factory=dict)  # (lang, lema/forma) -> sentidos


def _fail(where: str, msg: str):
    raise RulesError(f"{where}: {msg}")


def _obj(where: str, v, allowed: set[str] | None = None) -> dict:
    if not isinstance(v, dict):
        _fail(where, "debe ser un objeto.")
    if allowed is not None:
        bad = [k for k in v if not k.startswith("_") and k not in allowed]
        if bad:
            _fail(where, f"clave desconocida «{bad[0]}» (válidas: {', '.join(sorted(allowed))}).")
    return v


def _list(where: str, v) -> list:
    if not isinstance(v, list):
        _fail(where, "debe ser una lista.")
    return v


def _lang(where: str, v) -> str:
    if v not in LANGS:
        _fail(where, f"`lang` debe ser uno de {', '.join(LANGS)} (llegó {v!r}).")
    return v


def _word(where: str, v) -> str:
    if not isinstance(v, str) or len(v.split()) != 1 or not (n := normalize(v)):
        _fail(where, f"debe ser una sola palabra (llegó {v!r}).")
    return n


def _words(where: str, v) -> frozenset[str]:
    return frozenset(_word(f"{where}[{i}]", w) for i, w in enumerate(_list(where, v)))


def _cats(where: str, v, allow_empty: bool = False) -> tuple[str, ...]:
    v = _list(where, v)
    if not all(isinstance(c, str) and c.strip() for c in v) or (not v and not allow_empty):
        _fail(where, "debe ser una lista de nombres de categoría" + ("." if allow_empty else " (no vacía)."))
    return tuple(dict.fromkeys(c.strip() for c in v))


def _catmap(where: str, v) -> dict[str, tuple[str, ...]]:
    return {k: _cats(f"{where}.{k}", c, allow_empty=True) for k, c in _obj(where, v).items() if not k.startswith("_")}


def _pattern(where: str, v) -> tuple[str, ...]:
    if not isinstance(v, str):
        _fail(where, "`pattern` debe ser un texto.")
    toks = tuple(WILDCARD if p == WILDCARD else normalize(p) for p in v.split())
    if not toks or "" in toks:
        _fail(where, f"`pattern` vacío o con palabras sin letras (llegó {v!r}).")
    if all(t == WILDCARD for t in toks):
        _fail(where, "`pattern` necesita al menos una palabra además de «*».")
    return toks


def _cond(where: str, v) -> Cond | None:
    if v is None:
        return None
    d = _obj(where, v, {"prev", "next", "line_end", "line_start"})
    for k in ("line_end", "line_start"):
        if not isinstance(d.get(k, False), bool):
            _fail(where, f"`{k}` debe ser true o false.")
    c = Cond(_words(f"{where}.prev", d.get("prev", [])), _words(f"{where}.next", d.get("next", [])),
             d.get("line_end", False), d.get("line_start", False))
    if not (c.prev or c.next or c.line_end or c.line_start):
        _fail(where, "no tiene ninguna condición (prev, next, line_end, line_start).")
    return c


def _sense_cond(where: str, v) -> SenseCond:
    d = _obj(where, v, {"prev", "next", "near", "co_cats", "line_end", "line_start"})
    for k in ("line_end", "line_start"):
        if not isinstance(d.get(k, False), bool):
            _fail(where, f"`{k}` debe ser true o false.")
    sc = SenseCond(
        _words(f"{where}.prev", d.get("prev", [])),
        _words(f"{where}.next", d.get("next", [])),
        _words(f"{where}.near", d.get("near", [])),
        frozenset(_cats(f"{where}.co_cats", d.get("co_cats", []), allow_empty=True)),
        d.get("line_end", False),
        d.get("line_start", False),
    )
    if not (sc.prev or sc.next or sc.near or sc.co_cats or sc.line_end or sc.line_start):
        _fail(where, "no tiene ninguna condición (prev, next, near, co_cats, line_end, line_start).")
    return sc


def _pairs(where: str, v, item) -> list:
    out = []
    for i, p in enumerate(_list(where, v)):
        if not isinstance(p, list) or len(p) != 2:
            _fail(f"{where}[{i}]", "debe ser una pareja [a, b].")
        out.append((item(f"{where}[{i}][0]", p[0]), item(f"{where}[{i}][1]", p[1])))
    return out


def parse(raw: dict) -> Rules:
    """Valida el JSON con mensajes claros (el archivo lo edita la persona) y devuelve las reglas."""
    d = _obj("reglas", raw, _TOP)
    phrases = []
    for i, p in enumerate(_list("phrases", d.get("phrases", []))):
        w = f"phrases[{i}]"
        p = _obj(w, p, {"pattern", "lang", "categories", "at"})
        tokens = _pattern(w, p.get("pattern"))
        at = p.get("at", -1)
        if not isinstance(at, int) or isinstance(at, bool) or not -1 <= at < len(tokens):
            raise RulesError(f"{w}.at: debe ser -1 (última) o un índice de palabra del patrón (0..{len(tokens) - 1})")
        phrases.append(Phrase(tokens, _lang(w, p.get("lang")), _cats(f"{w}.categories", p.get("categories")), " ".join(tokens), at))
    negators = {_lang(f"negators.{k}", k): _words(f"negators.{k}", v)
                for k, v in _obj("negators", d.get("negators", {})).items() if not k.startswith("_")}
    negation = _catmap("negation", d.get("negation", {}))
    reversals = []
    for i, r in enumerate(_list("reversals", d.get("reversals", []))):
        w = f"reversals[{i}]"
        r = _obj(w, r, {"trigger", "side", "lang", "map"})
        trig = tuple(_word(f"{w}.trigger[{j}]", t) for j, t in enumerate(_list(f"{w}.trigger", r.get("trigger"))))
        if not trig:
            _fail(w, "`trigger` no puede estar vacío.")
        if r.get("side", "before") not in SIDES:
            _fail(w, f"`side` debe ser uno de {', '.join(SIDES)}.")
        mp = _catmap(f"{w}.map", r.get("map"))
        if not mp:
            _fail(w, "`map` no puede estar vacío.")
        reversals.append(Reversal(trig, r.get("side", "before"), _lang(w, r.get("lang")), mp))
    guards: dict[tuple[str, str], list[Guard]] = {}
    for i, g in enumerate(_list("pos_guards", d.get("pos_guards", []))):
        w = f"pos_guards[{i}]"
        g = _obj(w, g, {"word", "lang", "keep_if", "drop_if"})
        keep, drop = _cond(f"{w}.keep_if", g.get("keep_if")), _cond(f"{w}.drop_if", g.get("drop_if"))
        if not keep and not drop:
            _fail(w, "necesita `keep_if` o `drop_if`.")
        guards.setdefault((_lang(w, g.get("lang")), _word(f"{w}.word", g.get("word"))), []).append(Guard(keep, drop))
    ct = _obj("contrast", d.get("contrast", {}), {"categories", "words"})
    cpairs = _pairs("contrast.categories", ct.get("categories", []),
                    lambda w, v: _cats(w, [v])[0])
    wpairs = _pairs("contrast.words", ct.get("words", []), _word)
    senses: dict[tuple[str, str], list[SenseRule]] = {}
    for i, s in enumerate(_list("senses", d.get("senses", []))):
        w = f"senses[{i}]"
        s = _obj(w, s, {"word", "lang", "if", "categories", "label"})
        word = _word(f"{w}.word", s.get("word"))
        lang = _lang(w, s.get("lang"))
        cond = _sense_cond(f"{w}.if", s.get("if"))
        cats = _cats(f"{w}.categories", s.get("categories"))
        lbl = s.get("label", "")
        if not isinstance(lbl, str):
            _fail(f"{w}.label", "debe ser texto.")
        senses.setdefault((lang, word), []).append(SenseRule(word, lang, cond, cats, lbl.strip()))
    return Rules(tuple(phrases), negators, negation, tuple(reversals), {k: tuple(v) for k, v in guards.items()},
                 frozenset(frozenset(p) for p in cpairs), tuple(wpairs),
                 {k: tuple(v) for k, v in senses.items()})


def user_path() -> Path:
    from marea.paths import app_data  # perezoso: los tests de las reglas no necesitan Qt
    return app_data() / "wordfx" / "rules.json"


def load(path: Path | None = None) -> Rules:
    """`path` explícito, o la copia del usuario si existe, o la incluida. RulesError si el JSON no es válido."""
    if path is None:
        path = user_path()
        if not path.is_file():
            path = BUNDLED
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        if isinstance(e, RulesError):
            raise RulesError(f"{path.name}: {e}") from e
        raise RulesError(f"No pude leer {path.name}: {e}") from e
