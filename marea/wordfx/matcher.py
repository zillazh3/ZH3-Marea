"""Empareja las palabras de cada verso con la biblioteca: lema, idioma por palabra, reglas de contexto y límite de densidad.

La clave de una palabra es (índice del verso en `lyrics.lines`, índice de palabra del verso); las palabras se agrupan igual que
`marea.engine.engine._words` (una palabra termina donde `Syllable.word_end`), así que coinciden con `Item.words` del motor.
"""
from dataclasses import dataclass, field, replace

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import rules as rules_mod
from marea.wordfx.lexicon import guess_lang, lemma_candidates, line_lang, normalize
from marea.wordfx.library import Library
from marea.wordfx.rules import Cond, Phrase, Rules, SenseCond, SenseRule

MAX_PER_LINE = 2
NEGATION_WINDOW = 2  # palabras por delante en las que un negador afecta
HOLD_CAP_S = 3.0  # una nota larguísima no debe ganar siempre al de mayor prioridad
_NEG_CLITICS = {
    "es": frozenset({"me", "te", "se", "nos", "os", "lo", "la", "los", "las", "le", "les", "siquiera"}),
    "en": frozenset({"me", "you", "him", "her", "us", "them", "even", "ever", "to"}),
}
_NEG_BRIDGES = {
    "es": frozenset({
        "me", "te", "se", "nos", "os", "lo", "la", "los", "las", "le", "les", "a", "de", "en",
        "dejes", "dejas", "deja", "dejen", "hagas", "hace", "hacen",
        "puedo", "puedes", "puede", "podemos", "pueden", "pude", "podia",
        "quiero", "quieres", "quiere", "queremos", "quieren", "quise",
        "vuelvo", "vuelves", "vuelve", "vuelven", "vas", "va", "van", "voy", "vayas", "vaya",
        "siento", "sientes", "siente", "sentimos", "sienten", "veo", "ves", "ve", "vi",
        "tengo", "tienes", "tiene", "estoy", "estas", "esta", "soy", "eres", "es", "pido", "pide",
    }),
    "en": frozenset({
        "to", "be", "feel", "seem", "want", "need", "try", "get", "go", "stay",
        "even", "ever", "me", "you", "him", "her", "us", "them",
    }),
}
_LOWERCASE_GUARDED_MONTHS = frozenset({"may", "march"})
_EN_MONTH_CUES = frozenset({
    "in", "on", "of", "since", "from", "until", "till", "by", "before", "after", "through", "during",
    "every", "each", "next", "last", "this", "mid", "early", "late", "between", "into",
    "month", "day", "night", "morning", "evening", "rain", "wind", "breeze", "sun", "sky",
    "flower", "bloom", "moon", "light", "air", "sweet", "warm", "cold",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
})


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
    ambiguity: float = 0.0  # 0.0 = inequívoca; >0 = polisémica o multicategoría sin desambiguar


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


def _phrase_buckets(table: list[Phrase]) -> dict[str, list[tuple[int, Phrase]]]:
    buckets: dict[str, list[tuple[int, Phrase]]] = {}
    for idx, p in enumerate(table):
        buckets.setdefault(p.tokens[0], []).append((idx, p))
    return buckets


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


def _month_allowed(lib: Library, toks: list[_Tok], k: int, cand: str, cat: str) -> bool:
    """Months require exact normalized surface match (no inflection stems) and temporal cues for lowercase may/march."""
    if lib.categories[cat].kind != "month":
        return True
    tok = toks[k]
    if cand != tok.norm:
        return False
    if tok.lang == "en" and tok.norm in _LOWERCASE_GUARDED_MONTHS and tok.text.islower():
        has_prev = k > 0 and _has(toks[k - 1], _EN_MONTH_CUES, "en")
        has_next = k + 1 < len(toks) and _has(toks[k + 1], _EN_MONTH_CUES, "en")
        return has_prev or has_next
    return True


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
    lang = toks[k].lang
    negs = rules.negators.get(lang, ())
    if not negs:
        return None
    for j in range(k - 1, max(k - 1 - NEGATION_WINDOW, -1), -1):
        if j not in consumed and toks[j].norm in negs:
            return toks[j].norm
    # Allow a 3-token span only across clitic + grammatical bridge (e.g. "no me dejes caer", "never want to fall"),
    # keeping arbitrary intervening words like "no puedo ya respirar" or "no me perro amor" unnegated.
    j = k - 3
    if j >= 0 and j not in consumed and (k - 2) not in consumed and (k - 1) not in consumed and toks[j].norm in negs:
        if toks[k - 2].norm in _NEG_CLITICS.get(lang, ()) and toks[k - 1].norm in _NEG_BRIDGES.get(lang, ()):
            return toks[j].norm
    return None


def _sense_holds(sc: SenseCond, toks: list[_Tok], line_cats: dict[int, tuple[str, ...]], k: int, lang: str,
                 neg_deleted: set[int] | frozenset[int] = frozenset()) -> bool:
    n = len(toks)
    if sc.prev and not (k > 0 and _has(toks[k - 1], sc.prev, lang)):
        return False
    if sc.next and not (k + 1 < n and _has(toks[k + 1], sc.next, lang)):
        return False
    if sc.line_end and k != n - 1:
        return False
    if sc.line_start and k != 0:
        return False
    if sc.near and not any(m != k and m not in neg_deleted and _has(toks[m], sc.near, lang) for m in range(n)):
        return False
    if sc.co_cats and not any(m != k and any(c in sc.co_cats for c in cats) for m, cats in line_cats.items()):
        return False
    return True


def _senses_for(rules: Rules, tok: _Tok, lemma: str) -> list[SenseRule]:
    out: list[SenseRule] = []
    seen: set[int] = set()
    keys = (lemma, *(c for c in tok.cands() if c != lemma))
    for key in keys:
        for sr in rules.senses.get((tok.lang, key), ()):
            if id(sr) not in seen:
                seen.add(id(sr))
                out.append(sr)
    return out


def _sense(lib: Library, rules: Rules, toks: list[_Tok], line_cats: dict[int, tuple[str, ...]],
           k: int, lemma: str, neg_deleted: set[int] | frozenset[int] = frozenset()) -> tuple[tuple[str, ...], str, bool]:
    tok = toks[k]
    srules = _senses_for(rules, tok, lemma)
    for sr in srules:
        if _sense_holds(sr.cond, toks, line_cats, k, tok.lang, neg_deleted):
            if cats := _order(lib, sr.categories):
                lbl = sr.label or ",".join(cats)
                return cats, f"sense:{lbl}", True
    return (), "", bool(srules)


def _ambiguity(cats: tuple[str, ...], note: str, has_senses: bool) -> float:
    resolved = note.startswith(("phrase:", "rev:", "sense:"))
    if resolved:
        return 0.15 if (has_senses or len(cats) >= 2) else 0.0
    if has_senses:
        return round(min(1.0, 0.45 + 0.2 * max(len(cats) - 1, 0)), 2)
    if len(cats) >= 2:
        return round(min(1.0, 0.25 * (len(cats) - 1)), 2)
    return 0.0


def _contrast(rules: Rules, a: _Hit, b: _Hit) -> bool:
    if any(frozenset((x, y)) in rules.contrast_categories for x in a.match.categories for y in b.match.categories):
        return True
    return any((x in a.cands and y in b.cands) or (y in a.cands and x in b.cands) for x, y in rules.contrast_words)


def _note(m: Match, extra: str) -> str:
    return f"{m.note}; {extra}" if m.note else extra


def _pick_phrase(toks: list[_Tok], i: int, table: list[Phrase], buckets: dict[str, list[tuple[int, Phrase]]] | None) -> Phrase | None:
    if buckets is None:
        return next((p for p in table if _fits(toks, i, p)), None)
    seen: set[int] = set()
    cands: list[tuple[int, Phrase]] = []
    keys = dict.fromkeys((*toks[i].cands("es"), *toks[i].cands("en"), rules_mod.WILDCARD))
    for key in keys:
        if bucket := buckets.get(key):
            for idx, p in bucket:
                if idx not in seen:
                    seen.add(idx)
                    cands.append((idx, p))
    if not cands:
        return None
    if len(cands) > 1:
        cands.sort(key=lambda item: item[0])
    return next((p for _, p in cands if _fits(toks, i, p)), None)


def _match_line(line: Line, line_idx: int, lib: Library, max_fx: int, rules: Rules, table: list[Phrase],
                buckets: dict[str, list[tuple[int, Phrase]]] | None = None) -> list[Match]:
    toks = _tokens(line, lib.known)
    # 1. frases (multipalabra) ganan a las palabras sueltas; consumen sus tokens
    hits: list[_Hit] = []
    consumed: set[int] = set()
    line_cats: dict[int, tuple[str, ...]] = {}
    i = 0
    while i < len(toks):
        ph = _pick_phrase(toks, i, table, buckets)
        if not ph:
            i += 1
            continue
        j = i + len(ph.tokens)
        first, last = toks[i], toks[j - 1]
        key = toks[i + ph.at] if ph.at >= 0 else last  # palabra clave: la última, o la que marque `at`
        b0, b1 = (key.begin, key.end) if ph.at >= 0 else (first.begin, last.end)
        # shortcut: la frase aplica su efecto solo a la palabra clave, porque el plan reparte efectos por (verso, palabra)
        m = Match(line_idx, key.word, " ".join(t.text for t in toks[i:j]), ph.label, ph.lang, ph.categories, b0, b1,
                  _score(lib, ph.categories, b1 - b0), note=f"phrase:{ph.label}",
                  ambiguity=0.15 if len(ph.categories) >= 2 else 0.0)
        hits.append(_Hit(m, frozenset(key.cands(ph.lang)) | {ph.label}))
        for c_idx in range(i, j):
            line_cats[c_idx] = ph.categories
        consumed.update(range(i, j))
        i = j
    # 2. palabras sueltas: guardas de categoría gramatical, luego lema
    singles = {}
    for k, tok in enumerate(toks):
        if k in consumed or _guarded(rules, toks, k):
            continue
        # shortcut: una palabra con guion se prueba entera; partirla en piezas si aparecen compuestos en la letra
        hit = next(
            (
                (c, cats)
                for c in tok.cands()
                if c in lib.index[tok.lang]
                and (cats := tuple(cat for cat in lib.index[tok.lang][c] if _month_allowed(lib, toks, k, c, cat)))
            ),
            None,
        )
        if hit:
            cats_t = tuple(hit[1])
            singles[k] = (hit[0], cats_t, "")
            line_cats[k] = cats_t
    # 3a. Reversión («volver a respirar») primero, actualizando `line_cats`.
    neg_deleted: set[int] = set()
    for k in sorted(singles):
        if k not in singles:
            continue
        lemma, cats, _ = singles[k]
        if rev := _reversal(lib, rules, toks, consumed, k, cats):
            singles[k] = (lemma, rev[0], f"rev:{rev[2]}")
            line_cats[k] = rev[0]
            for j in rev[1]:
                singles.pop(j, None)
                line_cats.pop(j, None)
                neg_deleted.add(j)
    # 3b. Pre-negación en palabras sin sentidos contextuales para actualizar `line_cats` y `neg_deleted` antes de desambiguar.
    neg_hits: dict[int, str] = {}
    for k in sorted(singles):
        lemma, cats, note = singles[k]
        if note.startswith("rev:"):
            continue
        if (neg := _negator(rules, toks, consumed, k)) and any(c in rules.negation for c in cats):
            neg_hits[k] = neg
            if not (rules.senses and _senses_for(rules, toks[k], lemma)):
                new = _order(lib, [m for c in cats for m in rules.negation.get(c, (c,))])
                if new:
                    singles[k] = (lemma, new, f"neg:{neg}")
                    line_cats[k] = new
                    neg_deleted.add(k)
                else:
                    del singles[k]
                    line_cats.pop(k, None)
                    neg_deleted.add(k)
    # 3c. Sentidos contextuales sobre categorías activas del verso (también sobre palabras polisémicas aún por negar).
    sense_flags: dict[int, bool] = {}
    if rules.senses:
        for k in sorted(singles):
            lemma, cats, note = singles[k]
            if note.startswith(("rev:", "neg:")):
                continue
            scats, snote, has_s = _sense(lib, rules, toks, line_cats, k, lemma, neg_deleted)
            sense_flags[k] = has_s
            if scats:
                singles[k] = (lemma, scats, snote)
                line_cats[k] = scats
    # 3d. Negación final sobre palabras polisémicas ya desambiguadas (p.ej. «no arder de rabia»).
    for k, neg in neg_hits.items():
        if k not in singles:
            continue
        lemma, cats, note = singles[k]
        if note.startswith("neg:") or not any(c in rules.negation for c in cats):
            continue
        new = _order(lib, [m for c in cats for m in rules.negation.get(c, (c,))])
        if new:
            singles[k] = (lemma, new, f"neg:{neg}")
            line_cats[k] = new
            neg_deleted.add(k)
        else:
            del singles[k]
            line_cats.pop(k, None)
            neg_deleted.add(k)
    for k, (lemma, cats, note) in singles.items():
        tok = toks[k]
        has_s = sense_flags.get(k, bool(rules.senses and _senses_for(rules, tok, lemma)))
        hits.append(_Hit(Match(line_idx, tok.word, tok.text, lemma, tok.lang, cats, tok.begin, tok.end,
                               _score(lib, cats, tok.end - tok.begin), note=note,
                               ambiguity=_ambiguity(cats, note, has_s)), frozenset(tok.cands()) | {lemma}))
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
    table = phrase_table(lib, rules)
    return _match_line(line, line_idx, lib, max_fx, rules, table, _phrase_buckets(table))


def match_lyrics(lyrics: Lyrics, lib: Library, max_fx: int = MAX_PER_LINE, *, rules: Rules | None = None) -> dict[tuple[int, int], Match]:
    rules = rules if rules is not None else rules_mod.load()
    table = phrase_table(lib, rules)
    buckets = _phrase_buckets(table)
    out = {}
    for li, line in enumerate(lyrics.lines):
        for m in _match_line(line, li, lib, max_fx, rules, table, buckets):
            out[(m.line, m.word)] = m
    return out
