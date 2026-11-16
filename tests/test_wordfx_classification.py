"""Classification regressions for the bundled library + rules: inflections, new categories, known misfires."""
import pytest

from marea.model.lyrics import Line, Syllable
from marea.wordfx import library, rules as rules_mod
from marea.wordfx.matcher import match_line


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


@pytest.fixture(scope="module")
def rules():
    return rules_mod.load(rules_mod.BUNDLED)  # explicit: never the user's copy


def line_of(text: str, start=1000, per_word=400) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


@pytest.fixture(scope="module")
def run(lib, rules):
    def _run(text: str):
        return match_line(line_of(text), 0, lib, max_fx=9, rules=rules)
    return _run


@pytest.fixture(scope="module")
def cats(run):
    return lambda text: {c for m in run(text) for c in m.categories}


# --- (a) inflections keep their categories -----------------------------------

@pytest.mark.parametrize("text", ["llorar", "lloré", "llorando", "lloraba", "lágrimas", "lágrima"])
def test_llorar_family(cats, text):
    assert {"sadness"} <= cats(text)


@pytest.mark.parametrize("text", ["llorar", "lloré", "llorando", "lloraba"])
def test_llorar_verb_keeps_water(cats, text):
    assert {"sadness", "water"} <= cats(text)


@pytest.mark.parametrize("text", ["burn", "burning", "burned", "burnt"])
def test_burn_family(cats, text):
    assert {"loss", "fire"} <= cats(text)


@pytest.mark.parametrize("text", ["quemar", "quemé", "quemando"])
def test_quemar_family(cats, text):
    assert {"loss", "fire"} <= cats(text)


@pytest.mark.parametrize("text", ["amor", "amar", "amé", "amándote"])
def test_amor_family(cats, text):
    assert "love" in cats(text)


@pytest.mark.parametrize("text", ["shine", "shining", "shone"])
def test_shine_family(cats, text):
    assert {"joy", "light"} <= cats(text)


# --- (d) burning/burnt/burned resolve exactly like burn -------------------------

def test_burn_surface_forms_equal_burn(run):
    base = run("burn")[0]
    assert base.lemma == "burn"
    for form in ("burning", "burnt", "burned"):
        m = run(form)[0]
        assert (m.lemma, m.categories) == (base.lemma, base.categories), form


# --- (b) one positive test per new category, es + en ---------------------------

NEW_CATS = {
    "hope": (["la esperanza", "ilusión y fe"], ["hope and faith", "a new dawn"]),
    "loneliness": (["la soledad", "un vacío", "estoy solo"], ["so lonely", "alone and empty"]),
    "desire": (["el deseo", "la tentación"], ["pure desire", "sweet temptation"]),
    "freedom": (["libertad", "quiero volar"], ["freedom", "fly away"]),
    "light": (["la luz", "un destello"], ["the light", "a lantern"]),
    "electric": (["un relámpago", "el voltaje"], ["lightning", "voltage"]),
    "cold": (["el hielo", "la escarcha"], ["ice", "frost"]),
    "death": (["la muerte", "una tumba"], ["death", "a funeral"]),
    "scream": (["un grito", "rugir"], ["scream", "howl"]),
    "whisper": (["un susurro", "murmullo"], ["whisper", "murmur"]),
    "breath": (["el aliento", "exhalar"], ["breath", "exhale"]),
}


def test_new_categories_exist(lib):
    assert set(NEW_CATS) <= set(lib.categories)


@pytest.mark.parametrize("cat", NEW_CATS)
def test_new_category_spanish(cats, cat):
    for text in NEW_CATS[cat][0]:
        assert cat in cats(text), text


@pytest.mark.parametrize("cat", NEW_CATS)
def test_new_category_english(cats, cat):
    for text in NEW_CATS[cat][1]:
        assert cat in cats(text), text


@pytest.mark.parametrize("text, cat", [("la noche", "shadow"), ("tinieblas", "shadow"), ("midnight", "shadow"),
                                       ("la luna", "celestial"), ("the moon", "celestial"), ("nostalgia", "memory"),
                                       ("añoranza", "memory"), ("corazón", "love"), ("heartbeat", "love"),
                                       ("una flor", "spring"), ("petal", "spring"), ("blossom", "spring")])
def test_extended_categories(cats, text, cat):
    assert cat in cats(text)


# --- (c) misfires ----------------------------------------------------------------

@pytest.mark.parametrize("text", ["solo quiero verte", "solo tú", "es solo un juego", "solo para ti"])
def test_solo_as_only_has_no_fx(cats, text):
    got = cats(text)
    assert "celestial" not in got
    assert "loneliness" not in got


@pytest.mark.parametrize("text", ["estoy solo", "me siento sola", "tan solo", "me quedé sola", "voy a estar solo"])
def test_solo_as_alone_is_loneliness_not_celestial(cats, text):
    got = cats(text)
    assert "loneliness" in got
    assert "celestial" not in got


@pytest.mark.parametrize("text", ["do re mi fa sol", "sol mayor", "sol menor"])
def test_sol_musical_note_is_not_the_sun(cats, text):
    assert "celestial" not in cats(text)


@pytest.mark.parametrize("text", ["el sol", "eres mi sol", "solcito"])
def test_sol_is_the_sun(cats, text):
    assert "celestial" in cats(text)


@pytest.mark.parametrize("text", ["the sun rose", "she rose slowly", "the moon rose"])
def test_rose_verb_has_no_love(run, text):
    assert "rose" not in {m.text for m in run(text)}


@pytest.mark.parametrize("text", ["a rose", "red roses"])
def test_rose_flower_keeps_love(cats, text):
    assert "love" in cats(text)


@pytest.mark.parametrize("text", ["color rosa", "vestido rosa", "labios rosa", "rosa pastel"])
def test_rosa_colour_has_no_fx(run, text):
    assert "rosa" not in {m.text for m in run(text)}


def test_rosa_flower_keeps_love(cats):
    assert "love" in cats("una rosa")


@pytest.mark.parametrize("text", ["no seas cruel", "que seas feliz", "cuando seas grande", "seas lo que seas"])
def test_spanish_seas_is_not_water(cats, text):
    assert "water" not in cats(text)


@pytest.mark.parametrize("text", ["the seven seas", "over the seas"])
def test_english_seas_is_water(cats, text):
    assert "water" in cats(text)


@pytest.mark.parametrize("text", ["o sea", "lo que sea", "sea como sea"])
def test_spanish_sea_is_not_water(cats, text):
    assert "water" not in cats(text)


@pytest.mark.parametrize("text", ["we live", "she lives", "living", "live", "i am living", "who lives"])
def test_live_is_not_growth(cats, text):
    assert "growth" not in cats(text)


def test_lives_plural_noun_is_not_dropped_by_the_guard(run):
    assert "lives" in {m.text for m in run("nine lives")}


def test_cannot_breathe_is_anguish_and_breathing_again_is_healing(cats):
    assert cats("no puedo respirar") == {"anguish"}
    assert cats("volví a respirar") == {"healing"}
