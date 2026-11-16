"""Variants: deterministic, weighted, and never the same on two neighbouring FX words of a line."""
import pytest

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import config, library, variants
from marea.wordfx.composer import compose
from marea.wordfx.matcher import Match
from marea.wordfx.plan import build_plan
from marea.wordfx.variants import Variant


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


@pytest.fixture
def two_sadness_variants(monkeypatch):
    monkeypatch.setitem(variants.VARIANTS, "sadness", (
        Variant("base"), Variant("slow", sig={"waveform": "breath"}), Variant("drip", sig={"waveform": "tick"})))
    prev = config.FX
    yield
    config.set_fx(prev)


def line_of(text: str, start=1000, per_word=400) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def M(lemma, cat, line=0, word=0) -> Match:
    return Match(line, word, lemma, lemma, "es", (cat,), 1000, 2200)


def test_category_without_variants_has_only_base():
    assert [v.name for v in variants.options_for("time")] == ["base"]
    assert variants.pick("time", 0.99).name == "base"


def test_base_variant_is_the_unchanged_look(lib, two_sadness_variants):
    """Forcing the base variant must reproduce the look the category had before variants existed."""
    config.set_fx(config.with_fx(variant_weights={"sadness.slow": 0.0, "sadness.drip": 0.0}))
    with_variants = compose(M("llorar", "sadness"), lib, track_id="t")
    variants.VARIANTS.pop("sadness")
    assert with_variants == compose(M("llorar", "sadness"), lib, track_id="t") and with_variants.variant == "sadness.base"


def test_pick_is_deterministic_and_follows_weights(two_sadness_variants):
    assert variants.pick("sadness", 0.37).name == variants.pick("sadness", 0.37).name
    config.set_fx(config.with_fx(variant_weights={"sadness.base": 0.0, "sadness.slow": 0.0, "sadness.drip": 1.0}))
    assert all(variants.pick("sadness", u / 10).name == "drip" for u in range(10))


def test_pick_avoids_the_previous_variant(two_sadness_variants):
    for avoid in ("sadness.base", "sadness.slow", "sadness.drip"):
        assert all(variants.variant_id("sadness", variants.pick("sadness", u / 50, avoid).name) != avoid for u in range(50))


def test_variant_changes_the_motion_signature(lib, two_sadness_variants):
    config.set_fx(config.with_fx(variant_weights={"sadness.base": 0.0, "sadness.slow": 0.0, "sadness.drip": 1.0}))
    assert compose(M("llorar", "sadness"), lib, track_id="t").waveform == "tick"


def test_same_chorus_same_variant_and_neighbours_differ(lib, two_sadness_variants):
    lyrics = Lyrics([line_of("llorar lagrima"), line_of("llorar lagrima", start=9000)])
    for track in (f"song{i}" for i in range(40)):  # many seeds: the neighbour rule must hold for every one
        plan = build_plan(lyrics, lib, track_id=track)
        a, b, c, d = (plan.get(li, wi).params.variant for li in (0, 1) for wi in (0, 1))
        assert a != b and (a, b) == (c, d), (track, a, b, c, d)
