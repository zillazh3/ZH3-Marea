"""Coverage for curated bilingual lyric vocabulary (HUMBE, Josean Log, Esteman, Nasa Histoires)."""
import pytest

from marea.model.lyrics import Line, Syllable
from marea.wordfx import library
from marea.wordfx.matcher import match_line


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def line_of(text: str, start: int = 1000, per_word: int = 500) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def cats(lib, text: str) -> dict[str, tuple[str, ...]]:
    return {m.text: m.categories for m in match_line(line_of(text), 0, lib)}


@pytest.mark.parametrize("text,word,cat", [
    # HUMBE (es + en)
    ("tu belleza atemporal", "atemporal", "time"),
    ("timeless glow", "timeless", "time"),
    ("pura entropía", "entropía", "broken"),
    ("slow entropy", "entropy", "broken"),
    ("oro de quilates", "quilates", "craft"),
    ("gilded karat", "karat", "craft"),
    ("un amor sagrado", "sagrado", "tenderness"),
    ("sacred oath", "sacred", "tenderness"),
    ("destellas en la pista", "destellas", "celestial"),
    ("starburst above", "starburst", "celestial"),
    # Josean Log (es + en)
    ("tu elixir", "elixir", "healing"),
    ("purple jacaranda", "jacaranda", "growth"),
    ("fluyen en calma", "fluyen", "calm"),
    ("gentle cadence", "cadence", "calm"),
    ("la moraleja queda", "moraleja", "tenderness"),
    ("la conspiración", "conspiración", "anger"),
    ("tame the storm", "tame", "anger"),
    # Esteman (es + en)
    ("noche sensorial", "sensorial", "love"),
    ("sensual beat", "sensual", "love"),
    ("en la discoteca", "discoteca", "joy"),
    ("dazzling dancefloor", "dazzling", "joy"),
    ("robotizado y frío", "robotizado", "numbness"),
    ("unfeeling wall", "unfeeling", "numbness"),
    ("caminar extraño", "extraño", "loss"),
    ("a stranger now", "stranger", "loss"),
    # Nasa Histoires (es + en)
    ("una bugambilia", "bugambilia", "healing"),
    ("white bougainvillea", "bougainvillea", "healing"),
    ("el girasol", "girasol", "growth"),
    ("bright sunflower", "sunflower", "growth"),
    ("con espinas", "espinas", "anguish"),
    ("sharp thorns", "thorns", "anguish"),
    ("soledad de cactus", "cactus", "dryness"),
    ("parched earth", "parched", "dryness"),
    ("un forastero", "forastero", "longing"),
    ("lonely wanderer", "wanderer", "longing"),
    ("zarpó el marinero", "zarpó", "water"),
    ("the mariner", "mariner", "water"),
    ("floating dandelions", "dandelions", "air"),
    ("caen pétalos", "pétalos", "earth"),
    ("fallen petals", "petals", "earth"),
])
def test_artist_bilingual_lemmas_resolve(lib, text, word, cat):
    assert cat in cats(lib, text).get(word, ()), (text, word, cat)


@pytest.mark.parametrize("text,phrase,expected", [
    ("dando patadas de ahogado", "patadas de ahogado", {"anguish", "water"}),
    ("still treading water", "treading water", {"anguish", "water"}),
    ("una caótica belleza", "caótica belleza", {"growth", "anguish"}),
    ("such chaotic beauty", "chaotic beauty", {"growth", "anguish"}),
    ("vivir un amor libre", "amor libre", {"love", "growth"}),
    ("our free love", "free love", {"love", "growth"}),
    ("y te alejas", "te alejas", {"loss", "longing"}),
    ("as we drift apart", "drift apart", {"loss", "longing"}),
    ("doblo flor de papel", "flor de papel", {"craft", "loss"}),
    ("a paper flower", "paper flower", {"craft", "loss"}),
    ("soplo diente de león", "diente de león", {"air", "growth"}),
    ("camino sin certeza", "sin certeza", {"fear", "anguish"}),
    ("with no certainty", "no certainty", {"fear", "anguish"}),
    ("ready to set sail", "set sail", {"water", "longing"}),
    ("signos de guerra", "signos de guerra", {"anger", "sadness"}),
    ("old battle scars", "battle scars", {"anger", "sadness"}),
    ("como un agujero negro", "agujero negro", {"celestial", "shadow", "fear"}),
    ("inside a black hole", "black hole", {"celestial", "shadow", "fear"}),
    ("resurgir de las cenizas", "resurgir de las cenizas", {"growth", "fire"}),
    ("rise from the ashes", "rise from the ashes", {"growth", "fire"}),
])
def test_artist_bilingual_phrases_resolve(lib, text, phrase, expected):
    assert set(cats(lib, text).get(phrase, ())) == expected


@pytest.mark.parametrize("text,word,cat", [
    ("guardado en RESINA", "RESINA", "memory"),
    ("preserved in resin", "resin", "craft"),
    ("suave evocación", "evocación", "memory"),
    ("silencio y permanencia", "permanencia", "time"),
    ("último vestigio", "vestigio", "memory"),
    ("lento desvanecimiento", "desvanecimiento", "loss"),
    ("dulce reminiscencia", "reminiscencia", "memory"),
    ("paz y aceptación", "aceptación", "healing"),
    ("fina sutura", "sutura", "healing"),
    ("lenta restauración", "restauración", "healing"),
    ("mi reconstrucción", "reconstrucción", "healing"),
    ("lento resurgimiento", "resurgimiento", "healing"),
    ("detrás de la barricada", "barricada", "fear"),
    ("suave desnudez", "desnudez", "tenderness"),
    ("fría desconfianza", "desconfianza", "fear"),
    ("bajo el sauce", "sauce", "sadness"),
    ("sentir pertenencia", "pertenencia", "love"),
    ("firme arraigo", "arraigo", "earth"),
    ("fuerte vínculo", "vínculo", "love"),
    ("puro magnetismo", "magnetismo", "love"),
    ("luz de fotosíntesis", "fotosíntesis", "growth"),
    ("flores en el jardín", "jardín", "spring"),
    ("nueva germinación", "germinación", "growth"),
    ("en plena floración", "floración", "spring"),
    ("fuerte oleaje", "oleaje", "water"),
    ("profunda inmersión", "inmersión", "water"),
    ("triste desbordamiento", "desbordamiento", "water"),
    ("en una espiral", "espiral", "anguish"),
    ("mismo bucle", "bucle", "memory"),
    ("tanto agotamiento", "agotamiento", "numbness"),
    ("otra recaída", "recaída", "sadness"),
    ("sellamos un pacto", "pacto", "love"),
    ("eres mi kryptonita", "kryptonita", "anguish"),
    ("dulce tentación", "tentación", "love"),
    ("canto de sirenas", "sirenas", "longing"),
    ("aroma a vetiver", "vetiver", "calm"),
    ("sabor a amaretto", "amaretto", "intoxication"),
    ("olor a vainilla", "vainilla", "tenderness"),
    ("brilla la turmalina", "turmalina", "healing"),
    ("cerca del cuarzo", "cuarzo", "healing"),
    ("abiertas las grietas", "grietas", "broken"),
    ("a weeping willow", "willow", "sadness"),
    ("protective tourmaline", "tourmaline", "healing"),
])
def test_humbe_expanded_vocabulary_resolves(lib, text, word, cat):
    assert cat in cats(lib, text).get(word, ()), (text, word, cat)


def test_generic_low_signal_candidates_stay_excluded(lib):
    assert "latencia" not in cats(lib, "hay latencia en la red")
    assert "repetición" not in cats(lib, "pura repetición diaria")
