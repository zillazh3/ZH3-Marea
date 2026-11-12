"""Integración con la biblioteca y las reglas incluidas: los ejemplos de contexto y los 9 ejemplos de referencia (direcciones, no valores)."""
import colorsys

import pytest

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import library
from marea.wordfx.matcher import match_line, match_lyrics
from marea.wordfx.plan import build_plan


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def line_of(text: str, start=1000, per_word=500, last_ms=None) -> Line:
    """last_ms: duración de la última palabra (las notas sostenidas erosionan más)."""
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    if last_ms:
        syls[-1].end = syls[-1].begin + last_ms
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def cats(lib, text) -> dict[str, tuple]:
    return {m.text: m.categories for m in match_line(line_of(text), 0, lib)}


def fx(lib, *texts, last_ms=None, **kw):
    lines = [line_of(t, start=i * 10000, last_ms=last_ms) for i, t in enumerate(texts)]
    return build_plan(Lyrics(lines), lib, track_id="t", overrides=[], **kw)


def near(a, b, tol=12):  # el color del polo lo calcula el compositor sin el contagio del propio par: casi igual, no idéntico
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def luma(rgb):
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255


def sat(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[1]


# ---- reglas de contexto ----
def test_negation_reversal_and_phrases_from_the_spec(lib):
    assert cats(lib, "sin miedo")["miedo"] == ("growth",)
    assert cats(lib, "no puedo respirar")["respirar"] == ("anguish",)
    assert cats(lib, "volví a respirar") == {"respirar": ("healing",)}  # y «volver» no cuenta como anhelo
    assert cats(lib, "perder el tiempo contigo") == {"perder el tiempo contigo": ("longing", "love")}
    assert set(cats(lib, "vivir para siempre")["para siempre"]) == {"time", "tenderness"}
    assert cats(lib, "otra vez")["otra vez"] == ("longing",)


def test_contrast_pair_marks_both_words(lib):
    ms = {m.lemma: m for m in match_line(line_of("la cura y el veneno"), 0, lib)}
    assert ms["cura"].pair == ms["veneno"].word and ms["veneno"].pair == ms["cura"].word


def test_pos_guard_and_code_switching(lib):
    assert "solo" not in cats(lib, "solo quiero verte")
    ms = match_line(line_of("vuelvo a verte again"), 0, lib)
    assert {m.lang for m in ms} <= {"es", "en"}


# ---- los 9 ejemplos de referencia ----
def test_1_and_2_tiempo_perdido_bleed(lib):
    plan = fx(lib, "Fue tanto el tiempo perdido", last_ms=1200)  # «perdido» se sostiene 1.2 s
    tiempo, perdido = plan.get(0, 3).params, plan.get(0, 4).params
    assert tiempo.stagger_ms > 0 and tiempo.trail > 0 and tiempo.color_to != tiempo.color_from
    assert perdido.drift_em > 0.1 and perdido.erode > 0 and perdido.particles >= 10 and sat(perdido.color_from) < 0.5


def test_3_respirar_negated_is_anguished(lib):
    p = fx(lib, "no puedo respirar").get(0, 2).params
    assert p.jitter_em > 0 and p.spacing_em < 0 and p.chromatic_em > 0


def test_4_respirar_recovered_breathes_and_is_calm(lib):
    p = fx(lib, "volví a respirar").get(0, 2).params
    assert p.jitter_em == 0 and p.vector[0] > 0 and p.vector[1] < 0.5  # positivo y tranquilo, no angustiado


def test_5_forgetting_flickers_with_grain(lib):
    p = fx(lib, "quiero olvidar el pasado").get(0, 1)
    assert p is not None and p.match.categories[0] in ("memory", "loss")
    mem = fx(lib, "recordar el pasado").get(0, 0).params
    assert mem.grain > 0 and mem.opacity_min < 1


def test_6_origami_unfolds(lib):
    p = fx(lib, "un origami de papel").get(0, 1).params
    assert p.letter_offset_em > 0 and p.stagger_ms > 0


def test_7_contrast_pair_oscillates(lib):
    plan = fx(lib, "la cura y el veneno")
    a, b = plan.get(0, 1).params, plan.get(0, 4).params
    assert a.osc_hz == b.osc_hz == 2.0 and near(a.osc_color, b.color_from) and near(b.osc_color, a.color_from)
    assert (a.osc_phase, b.osc_phase) == (0.0, pytest.approx(3.14159, abs=1e-3)) and a.osc_jitter_em > 0


def test_8_repetition_grows_across_choruses(lib):
    plan = fx(lib, "vivir para siempre", "vivir para siempre", "vivir para siempre")
    a, b, c = (plan.get(i, 2).params for i in range(3))  # la frase vive en su última palabra
    assert (a.repeat_n, b.repeat_n, c.repeat_n) == (0, 1, 2)
    assert c.sparkle > 0 and a.sparkle == 0


def test_9_stars_sparkle(lib):
    p = fx(lib, "las estrellas").get(0, 1).params
    assert p.sparkle > 0 or "sparkle" in p.primitives


def test_clouds_have_their_own_category_and_float(lib):
    assert lib.index["es"]["nube"] == ["cloud"] and lib.index["en"]["cloud"] == ["cloud"]  # ya no se confunden con el vuelo
    assert cats(lib, "las nubes")["nubes"] == ("cloud",) and cats(lib, "a cloud of smoke")["cloud"] == ("cloud",)
    assert set(cats(lib, "vivo en las nubes")["en las nubes"]) == {"cloud", "dream"}  # frase hecha: nube + sueño
    assert set(cats(lib, "I am on cloud nine")["cloud nine"]) == {"cloud", "joy"}
    assert cats(lib, "no hay nubes") == {}  # negada se cancela
    p = fx(lib, "mi nube", last_ms=1500).get(0, 1).params
    assert p.drift_em < 0 and p.wave_em > 0 and p.trail > 0 and p.glow >= 0.3 and p.particles > 0  # sube despacio, ondula, eco y bruma
    assert luma(p.color_from) >= 0.7  # blanco azulado, no se pierde


def test_photography_words_have_their_own_category_and_focus_like_a_lens(lib):
    for text in ("la cámara", "una fotografía", "una polaroid", "el flash", "sin lente", "tu retrato", "a picture of you", "a camera", "the lens"):
        got = [c for w, c in cats(lib, text).items() if "photo" in c]
        assert got or "sin" in text, text  # «sin lente» cancela por negación: es lo esperado
    assert cats(lib, "una foto")["foto"] == ("photo",) and cats(lib, "tomar una foto") != {}
    assert set(cats(lib, "una foto mental")["foto mental"]) == {"memory", "photo"}  # frase: recuerdo + imagen
    assert cats(lib, "a negative result") == {}  # palabras ambiguas fuera: no disparan efecto
    p = fx(lib, "mi cámara", last_ms=1400).get(0, 1).params
    assert p.blur_em > 0 and p.glow >= 0.3 and p.grain > 0  # se enfoca, destella y lleva grano de película


def test_hurt_and_fire_forms_resolve(lib):
    for text, cat in (("herida", "sadness"), ("herido", "sadness"), ("hurting", "sadness"), ("hurt", "sadness"), ("lastimado", "sadness"),
                      ("fuego", "fire"), ("ardiendo", "fire"), ("arder", "fire"), ("ardía", "fire"), ("burnt", "fire"), ("burning", "fire"),
                      ("burned", "fire"), ("fire", "fire")):
        assert any(cat in c for c in cats(lib, text).values()), text


def test_new_everyday_words_resolve(lib):
    for text, word, cat in (("quiero vivir", "vivir", "growth"), ("mi vida", "vida", "growth"), ("poder verte", "verte", "love"), ("poder verte", "verte", "longing"),
                            ("la Tierra", "Tierra", "earth"), ("the earth", "earth", "earth"), ("to the zenith", "zenith", "celestial"),
                            ("my daily life", "daily", "time"), ("mi cenit", "cenit", "celestial")):
        assert cat in cats(lib, text).get(word, ()), (text, cat)


def test_confrontation_belief_and_feeling_words(lib):
    for text, word, cat in (("me jodiste", "jodiste", "anger"), ("eres egoísta", "egoísta", "anger"), ("lo afronté", "afronté", "growth"),
                            ("sigo creyendo", "creyendo", "growth"), ("quiero sentir", "sentir", "tenderness"), ("la realidad", "realidad", "earth")):
        assert cat in cats(lib, text).get(word, ()), (text, cat)
    assert cats(lib, "creo que sí") == {}  # muletilla, no fe
    assert cats(lib, "lo siento")["lo siento"] == ("sadness",)  # disculpa, no «sentir»
    assert len(cats(lib, "entre el sueño y la realidad")) == 2  # la pareja se conserva (tensión)


def test_relationship_jealousy_advice_and_assumption_words(lib):
    for text, word, cat in (("nuestra relación", "relación", "love"), ("mis experiencias", "experiencias", "memory"), ("tus celos", "celos", "anger"),
                            ("estás celosa", "celosa", "anger"), ("te lo recomiendo", "recomiendo", "tenderness"), ("supongo", "supongo", "dream"),
                            ("una suposición", "suposición", "dream"), ("poder retratarte", "retratarte", "photo"), ("mis realidades", "realidades", "earth")):
        assert cat in cats(lib, text).get(word, ()), (text, cat)
    assert cats(lib, "por supuesto") == {}  # «supuesto» no es suponer
    assert len(cats(lib, "entre la suposición y la realidad")) == 2  # la pareja se conserva (tensión)


def test_perfect_pretend_and_world_phrases(lib):
    assert "joy" in cats(lib, "es perfecto").get("perfecto", ())
    assert {"dream", "shadow"} <= set(cats(lib, "dejar de fingir").get("fingir", ()))
    assert set(cats(lib, "ya no te recuerdo")["no te recuerdo"]) == {"loss", "numbness"}
    ms = {k: m for k, m in match_lyrics(Lyrics([line_of("si el mundo acaba hoy")]), lib).items()}
    assert [(m.word, m.text) for m in ms.values()] == [(3, "mundo acaba hoy")]  # el efecto cae en «acaba», no en «hoy»
    ms = match_lyrics(Lyrics([line_of("el mundo te entrego")]), lib)
    assert [m.word for m in ms.values()] == [1] and set(next(iter(ms.values())).categories) == {"love", "celestial"}
