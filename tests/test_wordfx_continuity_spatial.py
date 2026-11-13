"""Checkpoint 3 tests: emotional momentum (Undertow), spatial/cross-line bleed, line handoff, ambient layer, bg & interlude tint."""
from types import SimpleNamespace

import pytest

from marea.model.lyrics import BgVocal, Interlude, Line, Lyrics, Syllable
from marea.wordfx import library
from marea.wordfx.bleed import BLEED_KERNEL, handoff_overlap_s, line_bleed_donors, spring_bleed_arrival
from marea.wordfx.envelope import sample_envelope
from marea.wordfx.matcher import Match
from marea.wordfx.momentum import MAX_AMBIENT_AMP, MAX_INTERLUDE_TINT, build_momentum, mood_tau_s
from marea.wordfx.plan import build_plan


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def _line(words: list[str], start_ms: int, step_ms: int = 350, bg: list[BgVocal] = ()) -> Line:
    syls = [
        Syllable(w, start_ms + i * step_ms, start_ms + (i + 1) * step_ms, word_end=i < len(words) - 1)
        for i, w in enumerate(words)
    ]
    return Line(syls[0].begin, syls[-1].end, syllables=syls, bg=list(bg))


def test_momentum_spring_tau_by_mood_and_monotonic_relaxation():
    sad_prof = SimpleNamespace(moods=("sad", "nostalgic"))
    euph_prof = SimpleNamespace(moods=("euphoric",))
    tau_sad = mood_tau_s(sad_prof)
    tau_euph = mood_tau_s(euph_prof)
    assert 2.4 <= tau_sad <= 3.0 and 1.5 <= tau_euph <= 1.9
    assert tau_sad > tau_euph

    # Critically damped step response: moves smoothly toward target without overshooting +0.9
    events = [(0.5, 2.5, (0.9, 0.2, -0.3, 0.7, 0.0, 0.0), 1.0)]
    curve = build_momentum(events, 5.0)
    vals = [curve.vector_at(t * 0.1)[0] for t in range(5, 30)]
    assert max(vals) <= 0.901
    assert curve.vector_at(2.0)[0] > 0.35


def test_spatial_and_cross_line_bleed_kernel():
    m0 = Match(0, 0, "amor", "amor", "es", ("love",), 1000, 1400)
    m3 = Match(0, 3, "luz", "luz", "es", ("light",), 2050, 2400)
    assert BLEED_KERNEL == {1: 0.35, 2: 0.12}
    # Same-line +-1 and +-2
    d1 = line_bleed_donors(0, 1, [m0, m3], None, 1000)
    assert any(d is m0 and w == pytest.approx(0.35) for d, w in d1)
    assert any(d is m3 and w == pytest.approx(0.12) for d, w in d1)
    # Cross-line bleed into next line word 0 (0.35) and word 1 (0.12)
    nxt0 = line_bleed_donors(1, 0, [], m3, 2700)
    nxt1 = line_bleed_donors(1, 1, [], m3, 2700)
    assert nxt0 == [(m3, 0.35)]
    assert nxt1 == [(m3, 0.12)]
    # Eases in via spring instead of jumping
    a0 = spring_bleed_arrival(1.9, 2.05, 2.4)
    a1 = spring_bleed_arrival(2.2, 2.05, 2.4)
    a2 = spring_bleed_arrival(2.4, 2.05, 2.4)
    assert a0 == 0.0 and 0.0 < a1 < a2 <= 1.0


def test_ambient_layer_populates_non_semantic_words_and_caps_at_15_pct(lib):
    lyrics = Lyrics([_line(["el", "dolor", "y", "la", "tristeza"], 1000)])
    plan = build_plan(lyrics, lib, track_id="amb", overrides=[])
    # plan.words keeps only semantic matches (backward-compatible)
    assert sorted(plan.words) == [(0, 1), (0, 4)]
    # Non-semantic words get ambient FxWord via word_fx <= 15%
    fx_el = plan.word_fx(0, 0)
    assert fx_el is not None and fx_el.params.note == "ambient"
    assert fx_el.params.fill_tint <= MAX_AMBIENT_AMP or fx_el.params.bleed > 0.0
    # Word with no neighbor within 2 tokens stays strictly <= 15%
    ly_far = Lyrics([_line(["la", "mesa", "de", "esta", "gran", "dolor"], 1000)])
    p_far = build_plan(ly_far, lib, track_id="far", overrides=[])
    fx_de = p_far.word_fx(0, 2)
    assert fx_de is not None and fx_de.params.fill_tint <= MAX_AMBIENT_AMP
    assert fx_de.params.glow <= MAX_AMBIENT_AMP


def test_line_handoff_overlap_keeps_continuous_singing_alive(lib):
    ol = handoff_overlap_s(2400, 2500)
    assert 0.25 <= ol <= 0.40
    # Outgoing line word ending at 2.4 s still has non-zero release/fade overlap at 2.55 s when incoming line begins anticipation
    e_out = sample_envelope(2.55, 2.0, 2.4, is_hero=True, line_end_s=2.4, fade_s=ol)
    e_in = sample_envelope(2.35, 2.5, 2.9, is_hero=True, line_end_s=3.2, fade_s=ol)
    assert e_out.motion_amp > 0.0 and e_in.motion_amp > 0.0


def test_background_vocals_and_interlude_tint(lib):
    bg = BgVocal(2600, 3400, [Syllable("amor", 2600, 3400, word_end=False)])
    lyrics = Lyrics([_line(["todo", "pasa"], 1000, bg=[bg]), _line(["otra"], 7000)])
    assert len(lyrics.interludes) >= 1
    plan = build_plan(lyrics, lib, track_id="bg_int", overrides=[])
    fx_bg = plan.word_fx(0, 2, is_bg=True)
    assert fx_bg is not None and fx_bg.params.is_bg and not fx_bg.params.is_hero
    # Compare bg glow vs main equivalent
    lyrics_main = Lyrics([_line(["amor"], 2600)])
    fx_main = build_plan(lyrics_main, lib, track_id="bg_int", overrides=[]).get(0, 0)
    assert fx_bg.params.glow < fx_main.params.glow

    # Interlude color stays subtle (<= 20% from pure white)
    col = plan.interlude_color(4.0)
    assert all(round(255 * (1.0 - MAX_INTERLUDE_TINT)) - 2 <= c <= 255 for c in col)
