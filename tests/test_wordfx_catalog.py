"""Catalog sanity checks for assets/wordfx/library.json (vocabulary only; no matcher, no Qt)."""
from collections import Counter

import pytest

from marea.wordfx import library
from marea.wordfx.lexicon import lemma_candidates, normalize

NEW_EMOTIONS = {"hope", "loneliness", "desire", "freedom", "death", "scream", "whisper", "breath"}
EMOTIONS = {"sadness", "anguish", "numbness", "loss", "longing", "memory", "tenderness", "love", "joy", "healing",
            "calm", "fear", "anger", "intoxication", "growth", *NEW_EMOTIONS}
ELEMENTS = {"time", "celestial", "air", "cloud", "photo", "earth", "water", "fire", "dryness", "dream", "shadow", "craft", "broken",
            "spring", "summer", "autumn", "winter", "light", "electric", "cold"}
MONTHS = {"january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"}

LEGACY = {  # kind, vector, primitives, priority: must never change
    "time": ("element", {"valence": -0.1, "arousal": 0.35, "gravity": 0.1, "temperature": 0.0, "fragility": 0.4, "blur": 0.3},
             ("letter_offset", "mask_reveal", "trail", "hue_shift"), 4),
    "loss": ("emotion", {"valence": -0.7, "arousal": 0.3, "gravity": 0.8, "temperature": -0.4, "fragility": 0.7, "blur": 0.3},
             ("gravity_drift", "desaturate", "erode", "particles"), 8),
    "love": ("emotion", {"valence": 0.8, "arousal": 0.45, "gravity": -0.3, "temperature": 0.6, "fragility": 0.4, "blur": 0.2},
             ("glow", "mask_reveal", "letter_offset", "displacement", "scale", "hue_shift"), 7),
}

FORMS = {
    "es": "perdido lágrimas llorando olvidé recuerdos besos abrazarte ahogándome temblando soñé despertando cicatrices estrellas heridas",
    "en": "lost tears crying forgotten kisses drowning dreams broken burning",
}


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def test_all_categories_present_with_kind(lib):
    assert set(lib.categories) == EMOTIONS | ELEMENTS | MONTHS
    for name, c in lib.categories.items():
        expected_kind = "emotion" if name in EMOTIONS else ("month" if name in MONTHS else "element")
        assert c.kind == expected_kind, name
        assert (6 <= c.priority <= 9) if c.kind == "emotion" else (3 <= c.priority <= 5), name
        assert 3 <= len(c.primitives) <= 6, name


def test_lemma_counts(lib):
    for name, c in lib.categories.items():
        need = (20 if name in NEW_EMOTIONS else 35) if name in EMOTIONS else (1 if name in MONTHS else 20)
        for lang in library.LANGS:
            assert len(c.lemmas.get(lang, ())) >= need, (name, lang, len(c.lemmas.get(lang, ())))


def test_no_duplicates_inside_category():
    import json
    raw = json.loads(library.BUNDLED.read_text(encoding="utf-8"))
    for name, d in raw.items():
        if name.startswith("_"):
            continue
        for lang, words in d["lemmas"].items():
            norm = [" ".join(normalize(p) for p in w.split()) for w in words]
            assert not [w for w, n in Counter(norm).items() if n > 1], (name, lang)


def test_legacy_categories_unchanged(lib):
    for name, (kind, vector, prims, prio) in LEGACY.items():
        c = lib.categories[name]
        assert (c.kind, c.vector, c.primitives, c.priority) == (kind, vector, prims, prio), name


def test_at_most_three_categories_per_lemma(lib):
    for lang, idx in lib.index.items():
        assert not {w: c for w, c in idx.items() if len(c) > 3}, lang


def test_pinned_single_category_lemmas(lib):
    assert lib.index["es"]["perder"] == ["loss"]
    assert lib.index["es"]["tiempo"] == ["time"]
    assert lib.index["es"]["amor"] == ["love"]


@pytest.mark.parametrize("word, cats", [("llorar", {"sadness", "water"}), ("fenix", {"growth", "fire"}),
                                       ("quemar", {"loss", "fire"}), ("temblar", {"anguish", "fear"})])
def test_multi_category_lemmas(lib, word, cats):
    assert cats <= set(lib.index["es"][word])


def test_contrast_partners_exist(lib):
    es = lib.index["es"]
    for w in ("cura", "veneno", "rapido", "lento", "sanidad", "devocion", "matar"):
        assert w in es, w
    assert "healing" in es["cura"] and "anger" in es["veneno"]


@pytest.mark.parametrize("lang", library.LANGS)
def test_surface_forms_resolve(lib, lang):
    for form in FORMS[lang].split():
        assert any(c in lib.index[lang] for c in lemma_candidates(form, lang)), (lang, form)
