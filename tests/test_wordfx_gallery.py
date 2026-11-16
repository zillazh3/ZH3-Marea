"""FX Gallery: debug dialog that renders one forced-FX word in a loop through the editor's own Scene/plan path."""
import pytest

from marea.ui.wordfx_gallery import ALL, WordFxGallery, config_snippet
from marea.wordfx import config, library, variants
from marea.wordfx.config import FxConfig

LIB = library.load(library.BUNDLED)


@pytest.fixture
def gal(qapp):
    before = config.FX
    g = WordFxGallery(LIB)
    yield g
    g.close()
    assert config.FX is before


def frame_in_word(g):
    """Render a frame in the middle of the forced word and return the plan word + frame."""
    g.preview.seek(700)  # the loop starts 0.5 s before the word: +700 ms is inside it
    return g.plan.words[(0, g._target)], g.preview._frame


def test_constructs_with_the_sample_word_forced_to_the_selected_category(gal):
    assert gal.cmb_cat.count() == len(LIB.categories) and gal.cats == sorted(LIB.categories)
    w, frame = frame_in_word(gal)
    assert w.match.categories == (gal.category,) and w.params.tier == "strong" and frame.size > 0 and gal.lbl_word.text()


def test_every_category_and_variant_renders_a_frame(gal):
    for cat in gal.cats:
        gal.cmb_cat.setCurrentIndex(gal.cats.index(cat))
        names = [v.name for v in variants.options_for(cat)]
        assert [gal.cmb_var.itemData(i) for i in range(gal.cmb_var.count())] == [*names, ALL]
        for i, name in enumerate(names):
            gal.cmb_var.setCurrentIndex(i)
            w, frame = frame_in_word(gal)
            assert w.params.variant == variants.variant_id(cat, name), (cat, name)
            assert frame.size > 0 and frame.any()


def test_all_variants_entry_cycles_one_variant_per_loop(gal):
    gal.cmb_cat.setCurrentIndex(gal.cats.index("joy"))
    gal.cmb_var.setCurrentIndex(gal.cmb_var.count() - 1)
    seen = []
    gal._on_position(1500.0, 1.0)
    for _ in range(len(variants.options_for("joy"))):
        seen.append(gal.plan.words[(0, 0)].params.variant)
        gal._on_position(0.0, 1.0)  # a loop boundary: position jumps back
        gal._on_position(1500.0, 1.0)
    assert len(set(seen)) == len(seen) > 1


def test_light_word_is_rendered_as_the_light_tier(gal):
    gal.cb_light.setChecked(True)
    w, frame = frame_in_word(gal)
    assert gal._target == 1 and w.params.tier == "light" and w.match.categories == (gal.category,) and frame.size > 0


def test_language_changes_the_sample_word(gal):
    gal.cmb_cat.setCurrentIndex(gal.cats.index("joy"))
    es = gal.lbl_word.text()
    gal.cmb_lang.setCurrentIndex(1)
    assert gal.lbl_word.text() != es and gal.plan.words[(0, 0)].match.lemma


def test_streak_slider_changes_the_active_config_and_the_plan(gal):
    gal.cmb_cat.setCurrentIndex(gal.cats.index("fear"))
    before = config.FX
    assert before.streak_by_category["fear"] == 0.0 and gal.plan.words[(0, 0)].params.streak == 0.0
    gal.rows["streak"].setValue(80)
    gal.rows["opacity"].setValue(55)
    assert config.FX is not before and config.FX.streak_by_category["fear"] == 0.8 and config.FX.streak_opacity == 0.55
    assert gal.plan.words[(0, 0)].params.streak > 0.0


def test_close_restores_the_config_that_was_active_before_opening(qapp):
    custom = FxConfig(glow_gain=2.0)
    prev = config.set_fx(custom)
    try:
        g = WordFxGallery(LIB)
        g.rows["glow"].setValue(150)
        assert config.FX is not custom
        g.close()
        assert config.FX is custom
    finally:
        config.set_fx(prev)


def test_copy_config_lists_only_fields_that_differ_from_the_defaults(gal, qapp):
    assert gal.copy_config() == "FxConfig()"
    gal.rows["glow"].setValue(120)
    gal.rows["opacity"].setValue(40)
    text = gal.copy_config()
    assert text == "FxConfig(glow_gain=1.5, streak_opacity=0.4)" and qapp.clipboard().text() == text
    gal.reset()
    assert gal.copy_config() == "FxConfig()" and config.FX == FxConfig(variant_weights=config.FX.variant_weights)
    assert config_snippet(FxConfig(streak_decay=3.0)) == "FxConfig(streak_decay=3.0)"
