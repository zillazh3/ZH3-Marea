"""Lematizador ES/EN basado en reglas y detector de idioma por token.

No genera flexiones: dado una forma de superficie devuelve posibles lemas
(generacion inversa); quien llama filtra por pertenencia a la biblioteca.
"""

from __future__ import annotations

import unicodedata

_MAX_CANDIDATES = 29


def normalize(s: str) -> str:
    """Minusculas sin acentos; conserva letras, digitos y apostrofes."""
    s = s.lower().replace("’", "'").replace("‘", "'")
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    return "".join(c for c in s if c.isalnum() or c == "'")


def _invert(src: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """lemma -> 'formas...' a forma -> lemas (en orden de aparicion)."""
    table: dict[str, list[str]] = {}
    for lemma, forms in src.items():
        for form in forms.split():
            if lemma not in table.setdefault(form, []):
                table[form].append(lemma)
    return {k: tuple(v) for k, v in table.items()}


# --- Espanol ---------------------------------------------------------------

_ES_IRREG = _invert({
    "ir": "voy vas va vamos van fui fuiste fue fuimos fueron iba ibas ibamos iban vaya",
    "ser": "soy eres es somos son sea seas fui fuiste fue fuimos fueron era eras eramos eran sido",
    "estar": "estoy estas esta estamos estan estuve estuvo estaba estabas estaban",
    "tener": "tengo tienes tiene tenemos tienen tuve tuvo tuvieron tenia tenias tenian",
    "poder": "puedo puedes puede podemos pueden pude pudo pudieron podia podias podria",
    "querer": "quiero quieres quiere queremos quieren quise quiso queria querias querria",
    "volver": "vuelvo vuelves vuelve vuelven volvi volvio vuelto",
    "perder": "pierdo pierdes pierde pierden",
    "sentir": "siento sientes siente sienten senti sintio",
    "dormir": "duermo duermes duerme durmio durmieron",
    "morir": "muero mueres muere murio muerto",
    "decir": "digo dices dice dijo dije dijeron dicho di",
    "hacer": "hago hace haces hizo hice hecho haz",
    "ver": "veo ves ve vi vio visto veia",
    "dar": "doy da das di dio dieron",
    "saber": "se sabe sabes supo supe sepa",
    "venir": "vengo vienes viene vino vine vienen ven",
    "poner": "pongo pones pone puso puse puesto pon",
    "suponer": "supongo supones supone suponemos suponen supuse supuso supusieron suponia",
    "caer": "caigo cae caes cayo cayeron",
    "oir": "oigo oye oyes oyo oido",
    "traer": "traigo trae traes trajo traje traido",
    "salir": "salgo sal",
    "conocer": "conozco",
    "romper": "roto",
    "abrir": "abierto",
    "escribir": "escrito",
    "volar": "vuelo vuelas vuela vuelan",
    "contar": "cuento cuentas cuenta",
    "sonar": "sueno suenas suena",
    "recordar": "recuerdo recuerdas recuerda",
    "encontrar": "encuentro encuentras encuentra",
    "pensar": "pienso piensas piensa",
    "empezar": "empiezo empiezas empieza empece",
    "cerrar": "cierro cierras cierra",
    "morder": "muerdo muerde",
    "doler": "duele dueles duelen dolia dolio dolera",
    "llover": "llueve",
    "arder": "ardo arde",
})

_CLITICS = ("nos", "les", "los", "las", "me", "te", "se", "lo", "la", "le")

# terminacion -> sufijos de infinitivo posibles (se prueban -er e -ir a la vez)
_AR, _ER_IR, _ALL = ("ar",), ("er", "ir"), ("ar", "er", "ir")
_VERB_END = {
    "o": _ALL, "a": _ALL, "as": _ALL, "es": _ALL, "e": _ALL, "an": _ALL, "en": _ALL,
    "amos": _ALL, "emos": _ALL,
    "aste": _AR, "aron": _AR, "ando": _AR, "aba": _AR, "abas": _AR, "aban": _AR, "abamos": _AR,
    "ado": _AR, "ada": _AR, "ados": _AR, "adas": _AR,
    "i": _ER_IR, "iste": _ER_IR, "io": _ER_IR, "imos": _ER_IR, "ieron": _ER_IR,
    "iendo": _ER_IR, "ido": _ER_IR, "ida": _ER_IR, "idos": _ER_IR, "idas": _ER_IR,
    "ia": _ER_IR, "ias": _ER_IR, "ian": _ER_IR, "iamos": _ER_IR,
    "yo": _ER_IR, "ye": _ER_IR, "yen": _ER_IR, "ya": _ER_IR, "yan": _ER_IR, "yendo": _ER_IR, "yeron": _ER_IR,
}
_VERB_END_ORDER = sorted(_VERB_END, key=lambda e: -len(e))
# terminaciones donde la raiz puede cambiar (duermo, pierdo, pidio, durmiendo...)
_STEM_CHANGE_END = {"o", "a", "as", "es", "e", "an", "en", "io", "ieron", "iendo"}
_STEM_RULES = (("ue", "o"), ("ie", "e"), ("ie", "i"), ("ue", "u"), ("i", "e"), ("u", "o"))
_FUTURE = ("iamos", "ias", "ian", "ia", "emos", "an", "as", "e", "a")
_VOWELS = "aeiou"


def _stem_variants(stem: str) -> list[str]:
    """Deshace el cambio vocalico de la ultima silaba (ue->o, ie->e, i->e...)."""
    out = []
    for a, b in _STEM_RULES:
        i = stem.rfind(a)
        j = i + len(a)
        if i < 0 or len(stem) - j > 3:
            continue
        if (j < len(stem) and stem[j] in _VOWELS) or (len(a) == 1 and i and stem[i - 1] in _VOWELS):
            continue  # la vocal debe estar aislada entre consonantes
        out.append(stem[:i] + b + stem[j:])
    return out


def _verb_lemmas(b: str) -> list[str]:
    res = []
    if len(b) >= 4 and b.endswith(("ar", "er", "ir")):
        res.append(b)
    for suf in _FUTURE:  # futuro/condicional = infinitivo + terminacion
        r = b[: -len(suf)]
        if b.endswith(suf) and len(r) >= 4 and r.endswith(("ar", "er", "ir")):
            res.append(r)
    for end in _VERB_END_ORDER:
        if not b.endswith(end) or len(b) - len(end) < 2:
            continue
        stem = b[: -len(end)]
        stems = [stem] + (_stem_variants(stem) if end in _STEM_CHANGE_END else [])
        res += [s + inf for s in stems for inf in _VERB_END[end]]
    return res


def _es_candidates(w: str) -> list[str]:
    out = [w]

    def add(x: str) -> None:
        if x and x not in out:
            out.append(x)

    bases, frontier = [w], [w]
    for _ in range(2):  # hasta dos clitico encadenados: dimelo -> dime -> di
        frontier = [b[: -len(c)] for b in frontier for c in _CLITICS
                    if b.endswith(c) and len(b) - len(c) >= 2]
        bases += frontier
    for b in bases:
        for lemma in _ES_IRREG.get(b, ()):
            add(lemma)
    for b in bases:
        for lemma in _verb_lemmas(b):
            add(lemma)

    sing = [w]
    if w.endswith("ces"):
        sing.append(w[:-3] + "z")
    if w.endswith("es") and len(w) > 3:
        sing.append(w[:-2])
    if w.endswith("s") and len(w) > 2:
        sing.append(w[:-1])
    for s in list(sing):  # diminutivos: gatito->gato, corazoncito->corazon
        for suf, reps in (("cito", ("", "e")), ("cita", ("", "a", "e")), ("ito", ("o",)), ("ita", ("a",))):
            if s.endswith(suf) and len(s) - len(suf) >= 2:
                sing += [s[: -len(suf)] + r for r in reps]
    for s in sing:
        add(s)
        if s.endswith("ona") or s.endswith("ora"):
            add(s[:-1])  # -ona/-ora -> -on/-or
        if s.endswith("a"):
            add(s[:-1] + "o")
    if w.endswith("mente") and len(w) > 7:
        r = w[:-5]
        add(r)
        if r.endswith("a"):
            add(r[:-1] + "o")
    return out


# --- Ingles ----------------------------------------------------------------

_EN_IRREG = _invert({
    "be": "was were been is are am being",
    "have": "had has having",
    "do": "did does done",
    "go": "went gone goes",
    "lose": "lost",
    "leave": "left",
    "feel": "felt",
    "fall": "fell fallen",
    "break": "broke broken",
    "fly": "flew flown",
    "sleep": "slept",
    "wake": "woke woken",
    "forget": "forgot forgotten",
    "burn": "burnt burned",
    "drink": "drank drunk",
    "run": "ran",
    "come": "came",
    "see": "saw seen",
    "say": "said",
    "think": "thought",
    "hold": "held",
    "keep": "kept",
    "swear": "swore sworn",
    "tear": "tore torn",
    "weep": "wept",
    "dream": "dreamt",
    "drown": "drowned",
    "sing": "sang sung",
    "begin": "began begun",
    "know": "knew known",
    "take": "took taken",
    "give": "gave given",
    "make": "made",
    "tell": "told",
    "find": "found",
    "stand": "stood",
    "grow": "grew grown",
    "hide": "hid hidden",
    "rise": "rose risen",
    "fight": "fought",
    "bleed": "bled",
    "shine": "shone",
    "wolf": "wolves",
    "life": "lives",
    "knife": "knives",
    "foot": "feet",
    "tooth": "teeth",
    "man": "men",
    "woman": "women",
    "child": "children",
})


def _undouble(s: str) -> str:
    return s[:-1] if len(s) > 2 and s[-1] == s[-2] else ""


def _en_candidates(w: str) -> list[str]:
    out = [w]

    def add(x: str) -> None:
        if len(x) >= 2 and x not in out:
            out.append(x)

    for lemma in _EN_IRREG.get(w, ()):
        add(lemma)
    if w.endswith("'s"):
        w = w[:-2]
    elif w.endswith("in'") and len(w) > 4:
        w = w[:-1] + "g"  # dancin' -> dancing
    if w != out[0]:
        add(w)
    if w.endswith("ies"):
        add(w[:-3] + "y")
    if w.endswith("ves"):
        add(w[:-3] + "f")
        add(w[:-3] + "fe")
    if w.endswith("es") and len(w) > 3:
        add(w[:-2])
    if w.endswith("s") and not w.endswith("ss"):
        add(w[:-1])
    if w.endswith("ied"):
        add(w[:-3] + "y")
    if w.endswith("ed") and len(w) > 3:
        add(_undouble(w[:-2]))
        add(w[:-1])
        add(w[:-2])
    if w.endswith("ing") and len(w) > 4:
        s = w[:-3]
        add(_undouble(s))
        if len(s) == 2 and s.endswith("y"):
            add(s[:-1] + "ie")  # dying -> die
        add(s + "e")
        add(s)
    for suf in ("est", "er"):  # comparativos/agentivos, version ligera
        if w.endswith(suf) and len(w) > len(suf) + 2:
            s = w[: -len(suf)]
            if s.endswith("i"):
                add(s[:-1] + "y")
            add(_undouble(s))
            add(s + "e")
            add(s)
    if w.endswith("ily"):
        add(w[:-3] + "y")
    if w.endswith("ly") and len(w) > 4:
        add(w[:-2])
    return out


def lemma_candidates(word: str, lang: str) -> list[str]:
    """Lemas posibles de `word` ('es'|'en'); el primero es siempre la forma normalizada."""
    w = normalize(word)
    if not w:
        return []
    if lang == "es":
        out = _es_candidates(w)
    elif lang == "en":
        out = _en_candidates(w)
    else:
        raise ValueError(f"unsupported lang: {lang!r}")
    return out[:_MAX_CANDIDATES]


# --- Idioma ----------------------------------------------------------------

_STOP_ES_RAW = """
el la los las un una unos unas lo de del al y que quien como cuando donde porque pero si mas muy ya
ni yo tu mi mis tus su sus te se nos le les con sin por para en sobre entre hasta desde ese esa
esto eso este esta aquel aqui alli ahi hoy siempre nunca nada todo toda todos tanto tan es soy eres
fue era hay tengo quiero voy vas va estoy bien asi tambien aun aunque entonces ahora despues antes
otra otro cada mucho poco algo nadie vez ella ellos nosotros solo no a me
"""
_STOP_EN_RAW = """
the and of to in is it you that she we they i my your his her our their me a an was were are am be
been will would can could shall should do does did don't not no with for on at by from this these
those there here what when where why how who which if but or so just like all one up down out over
never ever us as i'm you're it's can't won't ain't gonna wanna cause sin yo solo la
"""
_STOP_ES = set(_STOP_ES_RAW.split())
_STOP_EN = set(_STOP_EN_RAW.split())
# las palabras compartidas (no, a, me, la, sin, yo, solo...) no son evidencia
_ONLY_ES = _STOP_ES - _STOP_EN
_ONLY_EN = _STOP_EN - _STOP_ES
# shortcut: heuristicas de ortografia con falsos positivos raros (thriller, parking), subir a diccionario si molesta
_ES_SUFFIX = ("cion", "mente", "ado", "ido", "ando", "iendo")
_EN_SUFFIX = ("ing", "tion", "n't", "'m", "'ll", "'re", "'ve")
_EN_INSIDE = ("th", "ough")


def guess_lang(word: str, known: dict[str, set[str]] | None = None, fallback: str = "es") -> str:
    """'es' o 'en' para un token; `fallback` si no hay evidencia."""
    w = normalize(word)
    if not w:
        return fallback
    if known:
        hits = [lang for lang in ("es", "en")
                if any(c in known.get(lang, ()) for c in lemma_candidates(w, lang))]
        if len(hits) == 1:
            return hits[0]
    raw = word.lower()
    if any(c in raw for c in "ñ¿¡"):
        return "es"
    if len(w) >= 5 and w.endswith(_ES_SUFFIX) and not w.endswith(_EN_SUFFIX):
        return "es"
    if any(s in w for s in _EN_INSIDE) or (len(w) >= 5 and w.endswith(_EN_SUFFIX)) or w.endswith(_EN_SUFFIX[2:]):
        return "en"
    if any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", raw)):
        return "es"
    if w in _ONLY_ES:
        return "es"
    if w in _ONLY_EN:
        return "en"
    return fallback


def line_lang(words: list[str], known: dict[str, set[str]] | None = None) -> str:
    """Idioma dominante de una linea; empate -> 'es'."""
    votes = [guess_lang(w, known, fallback="") for w in words]
    return "en" if votes.count("en") > votes.count("es") else "es"
