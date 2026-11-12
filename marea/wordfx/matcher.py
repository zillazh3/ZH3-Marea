"""Empareja las palabras de cada verso con la biblioteca: lema, idioma por palabra, reglas de contexto y límite de densidad.

La clave de una palabra es (índice del verso en `lyrics.lines`, índice de palabra del verso); las palabras se agrupan igual que
`marea.engine.engine._words` (una palabra termina donde `Syllable.word_end`), así que coinciden con `Item.words` del motor.
"""
from dataclasses import dataclass, field, replace

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import rules as rules_mod
from marea.wordfx.lexicon import guess_lang, lemma_candidates, line_lang, normalize
from marea.wordfx.library import Library
from marea.wordfx.rules import Cond, Phrase, Rules

MAX_PER_LINE = 2
NEGATION_WINDOW = 2  # palabras por delante en las que un negador afecta
HOLD_CAP_S = 3.0  # una nota larguísima no debe ganar siempre al de mayor prioridad


@dataclass(frozen=True)
class Match:
    line: int
    word: int
    text: str  # forma tal como aparece en la letra
    lemma: str
    lang: str
    categories: tuple[str, ...]  # por prioridad desc.
    begin_ms: int
    end_ms: int
    score: float = 0.0
    pair: int = -1  # índice de palabra de su pareja de contraste en el mismo verso (-1 = ninguna)
    note: str = ""  # regla que decidió las categorías («neg:sin», «phrase:para siempre»…), solo depuración


def word_groups(syllables: list[Syllable]) -> list[list[Syllable]]:
    groups, cur = [], []
    for s in syllables:
        cur.append(s)
        if s.word_end:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups




@dataclass
class _Tok:
    word: int  # índice de palabra del verso (el de la clave del plan)
    text: str
    norm: str
    lang: str
    begin: int
    end: int
    _cands: dict = field(default_factory=dict, repr=False)

    def cands(self, lang: str | None = None) -> list[str]:
        lang = lang or self.lang
        if lang not in self._cands:
            self._cands[lang] = lemma_candidates(self.text, lang)
        return self._cands[lang]


@dataclass
class _Hit:  # candidato a recibir efecto, antes del límite de densidad
    match: Match
    cands: frozenset[str]  # formas/lemas de la palabra, para los pares de contraste por palabra


def _tokens(line: Line, known: dict[str, set[str]]) -> list[_Tok]:
    groups = word_groups(line.syllables)
    texts = ["".join(s.text for s in g) for g in groups]
    fallback = line_lang(texts, known)
    return [_Tok(wi, t, normalize(t), guess_lang(t, known, fallback), g[0].begin, g[-1].end)
            for wi, (g, t) in enumerate(zip(groups, texts)) if normalize(t)]


def _score(lib: Library, cats: tuple[str, ...], hold_ms: int) -> float:
    """Prioridad de la mejor categoría × cuánto se sostiene la palabra: elige qué palabras de un verso reciben efecto."""
    return max(lib.categories[c].priority for c in cats) * (1.0 + min(hold_ms / 1000, HOLD_CAP_S))


def _order(lib: Library, cats) -> tuple[str, ...]:
    """Sin duplicados, solo las que existen en la biblioteca (reglas y biblioteca evolucionan por separado), por prioridad desc."""
    return tuple(sorted(dict.fromkeys(c for c in cats if c in lib.categories), key=lambda c: -lib.categories[c].priority))


def phrase_table(lib: Library, rules: Rules) -> list[Phrase]:
    """Frases de las reglas y lemas de varias palabras de la biblioteca, las más largas primero (a igual largo ganan las reglas)."""
    out = []
    for p in rules.phrases:
        if cats := _order(lib, p.categories):
            out.append(replace(p, categories=cats))
    for lang, idx in lib.index.items():
        out += [Phrase(tuple(lemma.split()), lang, tuple(cats), lemma) for lemma, cats in idx.items() if " " in lemma]
    return sorted(out, key=lambda p: -len(p.tokens))


def _fits(toks: list[_Tok], i: int, ph: Phrase) -> bool:
    if i + len(ph.tokens) > len(toks):
        return False
    return all(p == rules_mod.WILDCARD or p in toks[i + n].cands(ph.lang) for n, p in enumerate(ph.tokens))


def _has(tok: _Tok, names: frozenset[str], lang: str) -> bool:
    return any(c in names for c in tok.cands(lang))


def _holds(c: Cond, toks: list[_Tok], k: int, lang: str) -> bool:
    n = len(toks)
    return bool((c.prev and k > 0 and _has(toks[k - 1], c.prev, lang)) or (c.next and k + 1 < n and _has(toks[k + 1], c.next, lang))
                or (c.line_end and k == n - 1) or (c.line_start and k == 0))


def _guarded(rules: Rules, toks: list[_Tok], k: int) -> bool:
    """True si alguna guarda descarta la palabra: se evalúan antes de asignar categorías."""
    tok = toks[k]
    for c in tok.cands():
        for g in rules.guards.get((tok.lang, c), ()):
            if (g.drop_if and _holds(g.drop_if, toks, k, tok.lang)) or (g.keep_if and not _holds(g.keep_if, toks, k, tok.lang)):
                return True
    return False


def _reversal(lib, rules, toks, consumed, k, cats):
    """(categorías, índices del disparador, etiqueta) si la palabra es una recuperación («volví a respirar»)."""
    lang = toks[k].lang
    for rv in rules.reversals:
        n = len(rv.trigger)
        lo = k - n if rv.side == "before" else k + 1
        if rv.lang != lang or lo < 0 or lo + n > len(toks):
            continue
        span = range(lo, lo + n)
        if any(j in consumed for j in span) or not all(t in toks[j].cands(lang) for t, j in zip(rv.trigger, span)):
            continue
        if new := _order(lib, [m for c in cats for m in rv.map.get(c, ())]):
            return new, span, " ".join(rv.trigger)
    return None


def _negator(rules: Rules, toks: list[_Tok], consumed: set[int], k: int) -> str | None:
    for j in range(k - 1, max(k - 1 - NEGATION_WINDOW, -1), -1):
        if j not in consumed and toks[j].norm in rules.negators.get(toks[k].lang, ()):
            return toks[j].norm
    return None


def _contrast(rules: Rules, a: _Hit, b: _Hit) -> bool:
    if any(frozenset((x, y)) in rules.contrast_categories for x in a.match.categories for y in b.match.categories):
        return True
    return any((x in a.cands and y in b.cands) or (y in a.cands and x in b.cands) for x, y in rules.contrast_words)


def _note(m: Match, extra: str) -> str:
    return f"{m.note}; {extra}" if m.note else extra


def _match_line(line: Line, line_idx: int, lib: Library, max_fx: int, rules: Rules, table: list[Phrase]) -> list[Match]:
    toks = _tokens(line, lib.known)
    # 1. frases (multipalabra) ganan a las palabras sueltas; consumen sus tokens
    hits: list[_Hit] = []
    consumed: set[int] = set()
    i = 0
    while i < len(toks):
        ph = next((p for p in table if _fits(toks, i, p)), None)
        if not ph:
            i += 1
            continue
        j = i + len(ph.tokens)
        first, last = toks[i], toks[j - 1]
        key = toks[i + ph.at] if ph.at >= 0 else last  # palabra clave: la última, o la que marque `at`
        b0, b1 = (key.begin, key.end) if ph.at >= 0 else (first.begin, last.end)
        # shortcut: la frase aplica su efecto solo a la palabra clave, porque el plan reparte efectos por (verso, palabra)
        m = Match(line_idx, key.word, " ".join(t.text for t in toks[i:j]), ph.label, ph.lang, ph.categories, b0, b1,
                  _score(lib, ph.categories, b1 - b0), note=f"phrase:{ph.label}")
        hits.append(_Hit(m, frozenset(key.cands(ph.lang)) | {ph.label}))
        consumed.update(range(i, j))
        i = j
    # 2. palabras sueltas: guardas de categoría gramatical, luego lema
    singles = {}
    for k, tok in enumerate(toks):
        if k in consumed or _guarded(rules, toks, k):
            continue
        # shortcut: una palabra con guion se prueba entera; partirla en piezas si aparecen compuestos en la letra
        hit = next(((c, lib.index[tok.lang][c]) for c in tok.cands() if c in lib.index[tok.lang]), None)
        if hit:
            singles[k] = (hit[0], tuple(hit[1]), "")
    # 3. reversión («volver a respirar») y negación («sin miedo»); la reversión consume su disparador
    for k in sorted(singles):
        if k not in singles:
            continue
        lemma, cats, _ = singles[k]
        if rev := _reversal(lib, rules, toks, consumed, k, cats):
            singles[k] = (lemma, rev[0], f"rev:{rev[2]}")
            for j in rev[1]:
                singles.pop(j, None)
        elif (neg := _negator(rules, toks, consumed, k)) and any(c in rules.negation for c in cats):
            new = _order(lib, [m for c in cats for m in rules.negation.get(c, (c,))])
            if new:
                singles[k] = (lemma, new, f"neg:{neg}")
            else:
                del singles[k]
    for k, (lemma, cats, note) in singles.items():
        tok = toks[k]
        hits.append(_Hit(Match(line_idx, tok.word, tok.text, lemma, tok.lang, cats, tok.begin, tok.end,
                               _score(lib, cats, tok.end - tok.begin), note=note), frozenset(tok.cands()) | {lemma}))
    hits.sort(key=lambda h: h.match.word)
    # 4. contraste: las parejas ocupan los huecos antes que cualquier otra palabra; mejor puntuación combinada primero
    chosen: list[Match] = []
    taken: set[int] = set()
    pairs = sorted(((a.match.score + b.match.score, x, y) for x, a in enumerate(hits) for y, b in enumerate(hits)
                    if x < y and _contrast(rules, a, b)), key=lambda p: (-p[0], p[1], p[2]))
    for _, x, y in pairs:
        if len(chosen) + 2 > max_fx:
            break
        if x in taken or y in taken:
            continue
        taken |= {x, y}
        a, b = hits[x].match, hits[y].match
        chosen += [replace(a, pair=b.word, note=_note(a, f"pair:{b.lemma}")), replace(b, pair=a.word, note=_note(b, f"pair:{a.lemma}"))]
    # 5. densidad: el resto de huecos por puntuación; desempate estable: la primera del verso
    rest = sorted((h.match for n, h in enumerate(hits) if n not in taken), key=lambda m: (-m.score, m.word))
    return sorted(chosen + rest[:max(max_fx - len(chosen), 0)], key=lambda m: m.word)


def match_line(line: Line, line_idx: int, lib: Library, max_fx: int = MAX_PER_LINE, *, rules: Rules | None = None) -> list[Match]:
    """`rules=None` carga las reglas del usuario o las incluidas; pasa `Rules()` para desactivarlas."""
    rules = rules if rules is not None else rules_mod.load()
    return _match_line(line, line_idx, lib, max_fx, rules, phrase_table(lib, rules))


def match_lyrics(lyrics: Lyrics, lib: Library, max_fx: int = MAX_PER_LINE, *, rules: Rules | None = None) -> dict[tuple[int, int], Match]:
    rules = rules if rules is not None else rules_mod.load()
    table = phrase_table(lib, rules)
    out = {}
    for li, line in enumerate(lyrics.lines):
        for m in _match_line(line, li, lib, max_fx, rules, table):
            out[(m.line, m.word)] = m
    return out
