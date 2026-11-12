import json

import pytest

from marea.model.lyrics import Line, Syllable
from marea.wordfx import library, rules
from marea.wordfx.matcher import match_line
from marea.wordfx.rules import Rules, RulesError

# categoría -> (prioridad, lemas es, lemas en)
CATS = {
    "fear": (7, ["miedo", "a ciegas"], ["fear"]),
    "growth": (6, ["valentia"], ["courage"]),
    "calm": (5, ["respirar", "calma"], ["breathe", "calm"]),
    "anguish": (9, ["angustia"], ["anguish"]),
    "healing": (6, ["cura", "curar"], ["cure", "heal"]),
    "anger": (7, ["veneno", "matar"], ["poison"]),
    "longing": (6, ["volver"], ["again"]),
    "love": (7, ["amor"], ["love"]),
    "time": (4, ["tiempo", "siempre", "rápido", "lento"], ["forever", "fast", "slow"]),
    "loss": (8, ["perder"], ["lose"]),
    "tenderness": (5, ["abrazo", "sentir"], ["hug"]),
    "sadness": (8, ["solo", "triste"], ["sad"]),
    "joy": (6, ["alegría"], ["joy"]),
    "intoxication": (4, ["vino", "tomar", "beber"], ["drink"]),
    "numbness": (5, ["no sentir nada"], ["numb", "still"]),
}


@pytest.fixture(scope="module")
def lib():
    return library.parse({n: {"lemmas": {"es": es, "en": en}, "vector": dict.fromkeys(library.AXES, 0.0), "primitives": ["glow"],
                              "priority": p} for n, (p, es, en) in CATS.items()})


@pytest.fixture(scope="module")
def bundled():
    return rules.load(rules.BUNDLED)


def line_of(text: str, start=1000, per_word=400) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def run(lib, rl, text, **kw):
    return match_line(line_of(text), 0, lib, rules=rl, **kw)


def cats(lib, rl, text, **kw):
    return [(m.text, m.categories) for m in run(lib, rl, text, **kw)]


# ---- frases ----

def test_phrase_beats_single_words_and_spans_all_tokens(lib, bundled):
    (m,) = run(lib, bundled, "perder el tiempo contigo")
    assert m.categories == ("love", "longing")  # por prioridad desc., no loss+time de las palabras sueltas
    assert (m.text, m.lemma, m.word, m.note) == ("perder el tiempo contigo", "perder el tiempo contigo", 3, "phrase:perder el tiempo contigo")
    assert (m.begin_ms, m.end_ms) == (1000, 1000 + 4 * 400)
    (m,) = run(lib, bundled, "perdí el tiempo contigo")  # la frase casa por lema
    assert m.word == 3


def test_seed_phrases(lib, bundled):
    assert cats(lib, bundled, "para siempre") == [("para siempre", ("tenderness", "time"))]
    assert cats(lib, bundled, "otra vez") == [("otra vez", ("longing",))]
    assert cats(lib, bundled, "for ever") == [("for ever", ("tenderness", "time"))]
    assert cats(lib, bundled, "forever and ever") == [("forever and ever", ("tenderness", "time"))]
    assert cats(lib, bundled, "just let go") == [("let go", ("loss",))]


def test_phrase_consumes_its_tokens(lib, bundled):
    ms = run(lib, bundled, "tiempo para siempre")
    assert [(m.word, m.note) for m in ms] == [(0, ""), (2, "phrase:para siempre")]  # «siempre» no casa además por su cuenta


def test_library_multiword_lemma_is_a_phrase(lib):
    (m,) = run(lib, Rules(), "vivo a ciegas hoy")
    assert (m.text, m.word, m.categories, m.note) == ("a ciegas", 2, ("fear",), "phrase:a ciegas")


def test_phrase_wildcard_matches_exactly_one_token(lib):
    rl = rules.parse({"phrases": [{"pattern": "perder * tiempo", "lang": "es", "categories": ["longing"]}]})
    assert cats(lib, rl, "perder el tiempo") == [("perder el tiempo", ("longing",))]
    assert [m.categories for m in run(lib, rl, "perder tiempo")] == [("loss",), ("time",)]
    assert [m.categories for m in run(lib, rl, "perder el mi tiempo")] == [("loss",), ("time",)]


def test_longest_phrase_wins(lib):
    rl = rules.parse({"phrases": [{"pattern": "otra vez", "lang": "es", "categories": ["longing"]},
                                  {"pattern": "otra vez mas", "lang": "es", "categories": ["love"]}]})
    assert cats(lib, rl, "otra vez mas") == [("otra vez mas", ("love",))]


def test_unknown_categories_in_rules_are_ignored(lib):
    rl = rules.parse({"phrases": [{"pattern": "otra vez", "lang": "es", "categories": ["bogus"]}],
                      "negators": {"es": ["sin"]}, "negation": {"fear": ["bogus"]}})
    assert run(lib, rl, "otra vez") == []
    assert run(lib, rl, "sin miedo") == []  # la sustitución inexistente anula, no revienta


# ---- negación ----

def test_negation_flips_or_cancels(lib, bundled):
    (m,) = run(lib, bundled, "sin miedo")
    assert (m.categories, m.note) == (("growth",), "neg:sin")
    (m,) = run(lib, bundled, "no puedo respirar")
    assert (m.categories, m.note) == (("anguish",), "neg:no")
    assert run(lib, bundled, "no estoy triste") == []  # sadness se anula
    assert cats(lib, bundled, "no puedo ya respirar") == [("respirar", ("calm",))]  # a 3 palabras no niega
    assert cats(lib, bundled, "he never fear") == [("fear", ("growth",))]
    assert cats(lib, Rules(), "sin miedo") == [("miedo", ("fear",))]  # sin reglas, nada cambia


def test_phrase_is_not_negated_again(lib, bundled):
    ms = run(lib, bundled, "no sentir nada")
    assert [(m.text, m.categories, m.note) for m in ms] == [("no sentir nada", ("numbness",), "phrase:no sentir nada")]


# ---- reversión ----

def test_reversal_turns_recovery_into_healing(lib, bundled):
    (m,) = run(lib, bundled, "volví a respirar")  # «volver» no casa aparte como longing
    assert (m.text, m.word, m.categories, m.note) == ("respirar", 2, ("healing",), "rev:volver a")
    (m,) = run(lib, bundled, "finally breathe")
    assert (m.categories, m.note) == (("healing",), "rev:finally")
    (m,) = run(lib, bundled, "breathe again")  # el disparador «again» va detrás y también se consume
    assert (m.text, m.categories) == ("breathe", ("healing",))


def test_reversal_only_applies_when_the_word_has_a_mapped_category(lib, bundled):
    assert cats(lib, bundled, "vuelvo a amor") == [("vuelvo", ("longing",)), ("amor", ("love",))]


# ---- guardas ----

@pytest.mark.parametrize("text,kept", [
    ("estoy solo", True), ("me siento solo", True), ("quiero vivir solo", True), ("solo quiero verte", False),
    ("el vino tinto", True), ("vino a verme", False), ("el vino a verme", False), ("tomar vino", True),
    ("la cura", True), ("el cura", False),
])
def test_pos_guards_es(lib, bundled, text, kept):
    target = {"solo": "solo", "vino": "vino", "cura": "cura"}[next(w for w in ("solo", "vino", "cura") if w in text)]
    assert any(m.text == target for m in run(lib, bundled, text, max_fx=5)) is kept


def test_tomar_only_counts_when_drinking(lib, bundled):
    assert [m.text for m in run(lib, bundled, "tomar mi mano", max_fx=5)] == []
    assert [m.text for m in run(lib, bundled, "tomar vino", max_fx=5)] == ["tomar", "vino"]
    assert [m.text for m in run(lib, bundled, "tomé un trago", max_fx=5)] == []  # shortcut documentado: solo mira la palabra siguiente


def test_pos_guard_en(lib, bundled):
    assert run(lib, bundled, "i still love you", max_fx=5)[0].text == "love"
    assert [m.text for m in run(lib, bundled, "i am still", max_fx=5)] == ["still"]


def test_guard_drop_wins_over_keep(lib):
    rl = rules.parse({"pos_guards": [{"word": "solo", "lang": "es", "keep_if": {"line_end": True}, "drop_if": {"prev": ["estar"]}}]})
    assert run(lib, rl, "estoy solo") == []
    assert [m.text for m in run(lib, rl, "vivir solo")] == ["solo"]


# ---- contraste ----

def test_contrast_words_keep_both_over_a_higher_priority_third(lib):
    rl = rules.parse({"contrast": {"words": [["cura", "veneno"]]}})
    assert [m.text for m in run(lib, Rules(), "angustia cura veneno")] == ["angustia", "veneno"]  # sin la regla, cura pierde
    a, b = run(lib, rl, "angustia cura veneno")
    assert (a.text, a.pair, a.note) == ("cura", 2, "pair:veneno")
    assert (b.text, b.pair, b.note) == ("veneno", 1, "pair:cura")


def test_contrast_words_compare_lemmas_and_languages(lib):
    rl = rules.parse({"contrast": {"words": [["rápido", "lento"], ["cure", "poison"]]}})
    a, b = run(lib, rl, "angustia rápida lento")
    assert (a.text, a.pair, b.text, b.pair) == ("rápida", 2, "lento", 1)
    a, b = run(lib, rl, "anguish cures poison")
    assert (a.text, a.pair, b.text, b.pair) == ("cures", 2, "poison", 1)


def test_contrast_categories(lib, bundled):
    a, b = run(lib, bundled, "angustia alegría triste")  # joy/sadness
    assert (a.text, a.pair, b.text, b.pair) == ("alegría", 2, "triste", 1)
    a, b = run(lib, bundled, "angustia cura veneno")  # healing/anger
    assert {(a.text, a.pair), (b.text, b.pair)} == {("cura", 2), ("veneno", 1)}


def test_best_scoring_pair_wins_and_a_word_gets_one_pair(lib, bundled):
    a, b = run(lib, bundled, "cura veneno alegría triste")  # joy+sadness (6+8) gana a healing+anger (6+7)
    assert (a.text, b.text) == ("alegría", "triste")
    rl = rules.parse({"contrast": {"words": [["cura", "veneno"], ["cura", "matar"]]}})
    ms = run(lib, rl, "cura veneno matar", max_fx=3)
    assert [(m.text, m.pair) for m in ms] == [("cura", 1), ("veneno", 0), ("matar", -1)]


def test_pair_needs_two_free_slots_and_both_words(lib, bundled):
    assert [m.pair for m in run(lib, bundled, "cura veneno", max_fx=1)] == [-1]
    assert [m.pair for m in run(lib, bundled, "cura")] == [-1]


# ---- densidad e idioma ----

def test_density_limit_and_order_still_apply(lib, bundled):
    ms = run(lib, bundled, "tiempo perder amor")
    assert [m.text for m in ms] == ["perder", "amor"] and all(m.pair == -1 for m in ms)
    assert len(run(lib, bundled, "tiempo perder amor angustia", max_fx=3)) == 3


def test_language_is_still_per_token_inside_a_phrase_line(lib, bundled):
    ms = run(lib, bundled, "otra vez again")
    assert [(m.text, m.lang, m.word) for m in ms] == [("otra vez", "es", 1), ("again", "en", 2)]


def test_match_default_rules_come_from_user_or_bundled(lib, monkeypatch, tmp_path):
    monkeypatch.setattr(rules, "user_path", lambda: tmp_path / "none.json")
    assert [m.categories for m in match_line(line_of("sin miedo"), 0, lib)] == [("growth",)]


# ---- carga y validación ----

def test_bundled_rules_validate(bundled):
    assert {p.label for p in bundled.phrases} >= {"perder el tiempo contigo", "para siempre", "otra vez", "let go"}
    assert "jamas" in bundled.negators["es"] and "don't" in bundled.negators["en"]
    assert bundled.negation["fear"] == ("growth",) and bundled.negation["calm"] == ("anguish",)
    assert ("es", "solo") in bundled.guards


@pytest.mark.parametrize("raw,msg", [
    ([], "reglas: debe ser un objeto"),
    ({"frases": []}, "clave desconocida «frases»"),
    ({"phrases": {}}, "phrases: debe ser una lista"),
    ({"phrases": [{"pattern": "", "lang": "es", "categories": ["a"]}]}, r"phrases\[0\]: `pattern` vacío"),
    ({"phrases": [{"pattern": "* *", "lang": "es", "categories": ["a"]}]}, "al menos una palabra"),
    ({"phrases": [{"pattern": "a b", "lang": "fr", "categories": ["a"]}]}, "`lang`"),
    ({"phrases": [{"pattern": "a b", "lang": "es", "categories": []}]}, "categories"),
    ({"phrases": [{"pattern": "a b", "lang": "es", "categories": ["a"], "extra": 1}]}, "clave desconocida «extra»"),
    ({"negators": {"es": "no"}}, "negators.es: debe ser una lista"),
    ({"negators": {"es": ["no se"]}}, "una sola palabra"),
    ({"negators": {"fr": ["non"]}}, "`lang`"),
    ({"negation": {"fear": "growth"}}, "negation.fear: debe ser una lista"),
    ({"reversals": [{"trigger": [], "lang": "es", "map": {"a": ["b"]}}]}, "`trigger` no puede estar vacío"),
    ({"reversals": [{"trigger": ["a"], "side": "mid", "lang": "es", "map": {"a": ["b"]}}]}, "`side`"),
    ({"reversals": [{"trigger": ["a"], "lang": "es", "map": {}}]}, "`map` no puede estar vacío"),
    ({"pos_guards": [{"word": "solo", "lang": "es"}]}, "necesita `keep_if` o `drop_if`"),
    ({"pos_guards": [{"word": "solo", "lang": "es", "keep_if": {}}]}, "ninguna condición"),
    ({"pos_guards": [{"word": "solo", "lang": "es", "keep_if": {"line_end": "si"}}]}, "true o false"),
    ({"pos_guards": [{"word": "solo", "lang": "es", "keep_if": {"after": ["a"]}}]}, "clave desconocida «after»"),
    ({"contrast": {"categories": [["a"]]}}, "pareja"),
    ({"contrast": {"words": [["a", 3]]}}, "una sola palabra"),
])
def test_invalid_rules_give_clear_errors(raw, msg):
    with pytest.raises(RulesError, match=msg):
        rules.parse(raw)


def test_load_reports_broken_json_and_user_copy_wins(tmp_path, monkeypatch):
    p = tmp_path / "rules.json"
    p.write_text("{no es json", encoding="utf-8")
    with pytest.raises(RulesError, match="rules.json"):
        rules.load(p)
    p.write_text(json.dumps({"phrases": [{"pattern": "x y", "lang": "es", "categories": ["a"]}]}), encoding="utf-8")
    with pytest.raises(RulesError, match=r"rules.json: phrases\[0\]") as e:
        p.write_text(json.dumps({"phrases": [{"pattern": " ", "lang": "es", "categories": ["a"]}]}), encoding="utf-8")
        rules.load(p)
    assert "vacío" in str(e.value)
    p.write_text(json.dumps({"phrases": [{"pattern": "x y", "lang": "es", "categories": ["a"]}]}), encoding="utf-8")
    monkeypatch.setattr(rules, "user_path", lambda: p)
    assert [ph.label for ph in rules.load().phrases] == ["x y"]
