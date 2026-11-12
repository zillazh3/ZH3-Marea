import colorsys
import hashlib
from dataclasses import fields

import pytest

from marea.wordfx import library
from marea.wordfx.composer import anchor_color, blend_categories, blend_palette, compose, seed_for
from marea.wordfx.fxtypes import AXES
from marea.wordfx.matcher import Match

GRAY = (154, 154, 154)
AMBER = [(255, 197, 107)]
AR5 = {"arousal": 0.5}  # justo en el umbral: la variación por seed no añade jitter (exige > 0.5)
OFF = {"jitter_em": 0, "wave_em": 0, "drift_em": 0, "scale_amp": 0, "rotation_deg": 0, "letter_offset_em": 0, "blur_em": 0,
       "glow": 0, "grain": 0, "chromatic_em": 0, "trail": 0, "erode": 0, "fracture": 0, "particles": 0, "sparkle": 0,
       "spacing_em": 0, "stagger_ms": 0, "jitter_hz": 0, "wave_hz": 0, "audio_gain": 0, "opacity_min": 1.0}


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def M(lemma, cat, hold_ms=1200, line=0, word=0) -> Match:
    return Match(line, word, lemma, lemma, "es", (cat,), 1000, 1000 + hold_ms)


def gray_dist(c):
    return sum(abs(x - g) for x, g in zip(c, GRAY))


def sat(c):
    return colorsys.rgb_to_hsv(*(x / 255 for x in c))[1]


def test_seed_for_is_stable_and_distinct():
    assert seed_for("t", 3, 4) == int.from_bytes(hashlib.sha1(b"t:3:4").digest()[:8], "big")
    assert len({seed_for("t", li, wi) for li in range(5) for wi in range(5)}) == 25
    assert seed_for("a", 0, 0) != seed_for("b", 0, 0)


def test_blend_categories_emotion_beats_element(lib):
    v = dict(zip(AXES, blend_categories(["time", "loss"], lib)))
    assert v["valence"] == pytest.approx((4 * -0.1 + 12 * -0.7) / 16)  # loss 8*1.5 contra time 4*1.0
    assert v["valence"] < -0.5
    assert blend_categories(["loss"], lib)[0] == pytest.approx(-0.7)


def test_blend_categories_priority_zero_does_not_divide_by_zero():
    lb = library.parse({"c": {"lemmas": {"es": ["x"]}, "vector": {**dict.fromkeys(library.AXES, 0.0), "valence": 0.5},
                              "primitives": ["glow"], "priority": 0}})
    assert blend_categories(["c"], lb)[0] == pytest.approx(0.5)


def test_deterministic_and_seed_changes_params(lib):
    a = compose(M("perdido", "loss"), lib, track_id="t", repeat_n=1)
    assert a == compose(M("perdido", "loss"), lib, track_id="t", repeat_n=1)
    other = compose(M("perdido", "loss", line=1, word=3), lib, track_id="t", repeat_n=1)
    assert other.seed != a.seed
    assert other != a
    assert compose(M("perdido", "loss"), lib, track_id="u") != compose(M("perdido", "loss"), lib, track_id="t")


def test_bleed_pulls_colour_and_vector_toward_neighbour(lib):
    tiempo, perdido = M("tiempo", "time", word=3), M("perdido", "loss", word=4)
    solo = compose(tiempo, lib, palette=AMBER)
    mixed = compose(tiempo, lib, palette=AMBER, neighbors=[(perdido, 1)])
    assert solo.bleed == 0
    assert mixed.bleed == pytest.approx(0.3) and mixed.bleed_from == "perdido"
    assert mixed.vector[0] < solo.vector[0]
    assert sat(mixed.color_to) < sat(mixed.color_from)  # hue_shift de «time» mueve el tono, así que se mide la saturación
    assert mixed.color_to != mixed.color_from
    # lejos (3 tokens) o con bleed=0 no contagia
    assert compose(tiempo, lib, palette=AMBER, neighbors=[(perdido, 3)]).bleed == 0
    assert compose(tiempo, lib, palette=AMBER, neighbors=[(perdido, 1)], bleed=0).bleed_from == ""


def test_no_bleed_no_primitive_keeps_colours_equal():
    lb = library.parse({"c": {"lemmas": {"es": ["x"]}, "vector": dict.fromkeys(library.AXES, 0.2), "primitives": ["glow"]}})
    p = compose(M("x", "c"), lb, palette=AMBER)
    assert p.color_to == p.color_from


def test_hue_shift_rotates_colour_to_by_temperature(lib):
    p = compose(M("amor", "love"), lib)  # love lista hue_shift
    assert p.color_to != p.color_from


def test_moods_shift_vector(lib):
    base = compose(M("tiempo", "time"), lib)
    sad = tuple([-1.0, 0.1, 0.5, -0.5, 0.5, 0.0])
    assert compose(M("tiempo", "time"), lib, song_mood=sad, line_mood=sad).vector[0] < base.vector[0]


def test_perdido_sinks_erodes_and_sheds_particles(lib):
    p = compose(M("perdido", "loss", 1200), lib)
    assert 0.19 <= p.drift_em <= 0.285
    assert 0.2 <= p.erode <= 0.4
    assert 16 <= p.particles <= 28
    assert p.hold_s == pytest.approx(1.2)
    short, long = compose(M("perdido", "loss", 400), lib), compose(M("perdido", "loss", 1500), lib)
    assert long.erode > short.erode and long.particles > short.particles and long.drift_em >= short.drift_em
    assert sat(p.color_to) < sat(compose(M("perdido", "loss"), lib, intensity=1.0).color_from) + 1e-9
    assert p.color_to[2] >= p.color_to[0]  # azulado
    assert sat(p.color_from) < 0.3  # el gris azulado de autor (#8A94A6) casi sin saturación


def test_love_unfolds_glows_and_rises(lib):
    p = compose(M("origami", "love"), lib)
    assert p.glow > 0 and p.drift_em < 0 and p.letter_offset_em > 0
    assert 30 <= p.stagger_ms <= 70
    assert p.color_from[0] > p.color_from[2]  # cálido


def test_beat_period_drives_stagger(lib):
    assert compose(M("amor", "love"), lib, beat_period_ms=320).stagger_ms == pytest.approx(80)
    assert compose(M("amor", "love"), lib, beat_period_ms=2000).stagger_ms == 140
    assert compose(M("amor", "love"), lib, beat_period_ms=40).stagger_ms == 40


def test_time_staggers_with_trail_and_inactive_primitives_stay_zero(lib):
    for w in range(8):  # incluye las variaciones por seed
        p = compose(M("tiempo", "time", word=w), lib)
        assert p.stagger_ms > 0 and p.trail > 0
        assert p.particles == p.erode == p.fracture == p.sparkle == 0 and p.grain == p.chromatic_em == p.drift_em == 0
        assert (p.glow > 0.3) == ("glow" in p.primitives)  # sin el primitivo solo queda el halo (0.3 con valencia ≤ 0)
        assert p.glow >= 0.3 and (p.jitter_em > 0) == ("jitter" in p.primitives)


def test_repeat_grows_intensity_and_adds_sparkle(lib):
    ps = [compose(M("amor", "love"), lib, repeat_n=n) for n in (0, 1, 2)]
    assert ps[0].scale_amp < ps[1].scale_amp < ps[2].scale_amp
    assert ps[1].scale_amp == pytest.approx(ps[0].scale_amp * 1.25)
    assert ps[0].sparkle == 0 and ps[1].sparkle == 0 and 4 <= ps[2].sparkle <= 9
    assert ps[2].repeat_n == 2
    assert compose(M("amor", "love"), lib, repeat_n=9).scale_amp == pytest.approx(ps[0].scale_amp * 2)  # tope x2


def test_intensity_zero_turns_everything_off_but_keeps_colour(lib):
    on = compose(M("perdido", "loss"), lib, repeat_n=2)
    off = compose(M("perdido", "loss"), lib, repeat_n=2, intensity=0)
    assert {f.name: getattr(off, f.name) for f in fields(off) if f.name in OFF} == OFF
    assert (off.color_from, off.color_to, off.vector, off.seed) == (on.color_from, on.color_to, on.vector, on.seed)


def test_intensity_scales_amplitude(lib):
    lo, mid, hi = (compose(M("perdido", "loss"), lib, intensity=i) for i in (0.5, 1.0, 1.5))
    assert abs(lo.drift_em) < abs(mid.drift_em) < abs(hi.drift_em)
    assert lo.erode < mid.erode < hi.erode
    assert lo.seed == hi.seed


def test_blend_palette():
    assert blend_palette((100, 100, 100), []) == (100, 100, 100)
    assert blend_palette((100, 0, 0), [(0, 0, 100), (0, 100, 100)]) == (60, 20, 40)  # 0.6*rgb + 0.4*media
    assert blend_palette((10, 20, 30), [(200, 200, 200)], 1.0) == (10, 20, 30)


def test_anchor_color_regions():
    cold = anchor_color(-0.7, -0.6, 0.5)
    assert cold[2] > cold[0] and max(cold) < 160  # azulado y oscuro
    warm = anchor_color(0.7, 0.5, 0.5)
    assert warm[0] > warm[2] and sum(warm) / 3 > 170  # cálido y brillante
    assert sat(anchor_color(0, 0, 0.5)) < 0.05
    assert sat(anchor_color(0.7, 0.5, 0.1)) < sat(anchor_color(0.7, 0.5, 0.9))  # poca arousal -> más gris
    assert all(0 <= x <= 255 for x in anchor_color(1, 1, 1) + anchor_color(-1, -1, 0))


def test_halo_glow_on_every_word_and_off_at_zero_intensity(lib):
    for cat in ("time", "loss", "love"):
        assert compose(M("x", cat), lib).glow >= 0.3
        assert compose(M("x", cat), lib, intensity=0).glow == 0
    assert compose(M("x", "loss"), lib, intensity=1.5).glow == pytest.approx(0.45)  # perdido no lista glow: solo halo (0.3 x 1.5)


def test_vibrance_boosts_saturation_but_not_desaturate_or_numb(lib):
    lb = library.parse({"c": {"lemmas": {"es": ["x"]}, "vector": {**dict.fromkeys(library.AXES, 0.0), "valence": 0.7,
                                                                  "arousal": 0.5, "temperature": -0.7},
                              "primitives": ["glow"]}})
    p = compose(M("x", "c"), lb)
    assert sat(p.color_from) > sat(anchor_color(0.7, -0.7, 0.5)) + 0.05  # cian saturado: más vivo que el ancla
    numb = compose(M("x", "c"), lb, vector_override={"arousal": 0.1})
    assert sat(numb.color_from) < 0.2
    assert sat(compose(M("perdido", "loss"), lib).color_from) < 0.35  # desaturate: sigue grisáceo


def test_vector_override_replaces_and_clamps_axes(lib):
    base = compose(M("perdido", "loss"), lib)
    p = compose(M("perdido", "loss"), lib, vector_override={"valence": 5.0, "arousal": -2.0})
    assert p.vector[0] == 1.0 and p.vector[1] == 0.0
    assert p.vector[2:] == base.vector[2:]
    assert compose(M("perdido", "loss"), lib, vector_override={"gravity": -0.5}).drift_em < 0 < base.drift_em
    with pytest.raises(ValueError):
        compose(M("perdido", "loss"), lib, vector_override={"mood": 1})


def test_note_is_copied(lib):
    m = Match(0, 0, "perdido", "perder", "es", ("loss",), 0, 1200, note="neg:sin")
    assert compose(m, lib).note == "neg:sin" and compose(m, lib, intensity=0).note == "neg:sin"


def test_partner_oscillation(lib):
    cura, veneno = M("cura", "love", word=1), M("veneno", "loss", word=2)
    a = compose(cura, lib, partner=veneno, osc_phase=0.0)
    b = compose(veneno, lib, partner=cura, osc_phase=3.14159)
    assert a.osc_hz == b.osc_hz == 2.0 and (a.osc_phase, b.osc_phase) == (0.0, 3.14159)
    assert a.osc_color == b.color_from and b.osc_color == a.color_from  # cada una oscila hacia el color de la otra
    assert a.osc_jitter_em == pytest.approx(0.03) and a.audio_gain >= 0.3 and b.audio_gain >= 0.3
    assert compose(cura, lib, partner=veneno, intensity=1.5).osc_jitter_em == pytest.approx(0.045)
    off = compose(cura, lib, partner=veneno, intensity=0, osc_phase=1.0)
    assert (off.osc_hz, off.osc_jitter_em, off.osc_phase, off.audio_gain) == (0, 0, 0, 0) and off.osc_color == a.osc_color
    solo = compose(cura, lib)
    assert (solo.osc_hz, solo.osc_jitter_em, solo.osc_color) == (0.0, 0.0, (255, 255, 255))


def luma(rgb):
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255


def test_authored_colors_win_over_the_vector_and_keep_their_endpoints(lib):
    p = compose(M("origami", "craft"), lib)  # rosa pálido -> rosa intenso, sin mezclarse con una portada (paleta vacía)
    assert p.color_from == (255, 217, 226) and p.color_to[0] == 255 and p.color_to[1] >= 111  # el suelo de luma lo aclara un poco
    tinted = compose(M("origami", "craft"), lib, palette=[(20, 30, 200)])  # con portada azul: un 8 %, no un 40 %
    assert tinted.color_from != p.color_from and tinted.color_from[0] > 200


def test_positive_words_get_more_light_so_they_do_not_dim_next_to_white_text(lib):
    for cat in ("celestial", "joy", "love"):
        p = compose(M("x", cat), lib, palette=[(40, 50, 170)])  # portada azul oscura
        assert luma(p.color_from) >= 0.68 and luma(p.color_to) >= 0.68 and p.glow >= 0.4, cat


def test_library_color_is_validated():
    base = {"lemmas": {"es": ["x"]}, "vector": dict.fromkeys(library.AXES, 0.0), "primitives": ["glow"]}
    assert library.parse({"c": {**base, "color": ["#102030", "#ffffff"]}}).categories["c"].color == ((16, 32, 48), (255, 255, 255))
    for bad in (["azul"], ["#12345"], ["#000000", "#111111", "#222222"], 7):
        with pytest.raises(library.LibraryError, match="color"):
            library.parse({"c": {**base, "color": bad}})


def test_raw_vocals_strain_gain_tiers(lib):
    off = compose(M("x", "anguish"), lib)
    assert off.strain_gain == 0.0 and off == compose(M("x", "anguish"), lib, raw_vocals=False)
    assert compose(M("x", "anguish"), lib, raw_vocals=True).strain_gain == pytest.approx(0.85)
    assert compose(M("x", "fear"), lib, raw_vocals=True).strain_gain == pytest.approx(0.85)
    assert compose(M("x", "fire"), lib, raw_vocals=True).strain_gain == pytest.approx(0.85)  # lista jitter
    assert compose(M("x", "joy"), lib, raw_vocals=True, vector_override=AR5).strain_gain == pytest.approx(0.5)  # emoción, arousal 0.5, sin jitter/chromatic
    assert compose(M("x", "tenderness"), lib, raw_vocals=True).strain_gain == 0.0  # arousal baja
    assert compose(M("x", "celestial"), lib, raw_vocals=True).strain_gain == 0.0  # elemento
    assert compose(M("x", "anguish"), lib, raw_vocals=True, intensity=1.5).strain_gain == 1.0
    assert compose(M("x", "anguish"), lib, raw_vocals=True, intensity=0.5).strain_gain == pytest.approx(0.425)
    assert compose(M("x", "anguish"), lib, raw_vocals=True, intensity=0).strain_gain == 0.0


def test_raw_vocals_adds_jitter_chromatic_when_missing_and_boosts_anguish(lib):
    p = compose(M("x", "joy"), lib, raw_vocals=True, vector_override=AR5)
    assert {"chromatic", "jitter"} <= set(p.primitives)
    assert p.chromatic_em >= 0.02 and p.jitter_em >= 0.01 and p.audio_gain >= 0.4
    assert compose(M("x", "joy"), lib, vector_override=AR5).chromatic_em == 0  # sin raw_vocals no cambia nada
    on, off = compose(M("x", "anguish"), lib, raw_vocals=True), compose(M("x", "anguish"), lib)
    assert on.jitter_em == pytest.approx(off.jitter_em * 1.2) and on.chromatic_em == pytest.approx(off.chromatic_em * 1.2)
    assert on == compose(M("x", "anguish"), lib, raw_vocals=True)


def test_positive_and_bright_words_flash_harder_than_dull_ones():
    from marea.wordfx import library
    from marea.wordfx.plan import build_plan
    from tests.test_wordfx_phase4 import line_of
    from marea.model.lyrics import Lyrics
    lib = library.load(library.BUNDLED)
    flash = {w: build_plan(Lyrics([line_of(f"mi {w}")]), lib, track_id="t", overrides=[]).get(0, 1).params.flash for w in ("alegría", "estrella", "llorar")}
    assert flash["alegría"] > flash["llorar"] and flash["estrella"] > flash["llorar"]
