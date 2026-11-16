from __future__ import annotations

import math
import time
import pytest

from marea.demo_cover import demo_cover
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import library, rules
from marea.wordfx.anim import letter_state, particles
from marea.wordfx.composer import compose
from marea.wordfx.config import FX
from marea.wordfx.matcher import Match, match_line
from marea.wordfx.plan import build_plan
from marea.wordfx.signatures import SIGNATURES, eval_waveform

MONTHS: tuple[str, ...] = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

ES_MONTH_FORMS: dict[str, str] = {
    "january": "enero",
    "february": "febrero",
    "march": "marzo",
    "april": "abril",
    "may": "mayo",
    "june": "junio",
    "july": "julio",
    "august": "agosto",
    "september": "septiembre",
    "october": "octubre",
    "november": "noviembre",
    "december": "diciembre",
}

EN_MONTH_LINES: dict[str, str] = {
    "january": "in january morning",
    "february": "cold february night",
    "march": "march rain falling",
    "april": "in april morning",
    "may": "since may arrived",
    "june": "golden june sky",
    "july": "bright july heat",
    "august": "warm august sun",
    "september": "quiet september dusk",
    "october": "dark october wind",
    "november": "gray november fog",
    "december": "still december snow",
}


def _line(text: str, start: int = 0, dur: int = 1500) -> Line:
    ws = text.split()
    step = max(1, dur // max(1, len(ws)))
    syls = [
        Syllable(w, start + i * step, start + (i + 1) * step, word_end=i < len(ws) - 1)
        for i, w in enumerate(ws)
    ]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def _compose_month(lib, month: str, dur_ms: int = 1600, begin_ms: int = 1000):
    m = Match(
        line=0,
        word=0,
        text=month,
        lemma=month,
        lang="en",
        categories=(month,),
        begin_ms=begin_ms,
        end_ms=begin_ms + dur_ms,
        score=5.0,
    )
    return compose(m, lib, track_id="month-unit-test", line_mood=(0.0, 0.5, 0.0, 0.0, 0.0, 0.0))


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


@pytest.fixture(scope="module")
def rule_set():
    return rules.load(rules.BUNDLED)


@pytest.mark.parametrize("month", MONTHS)
def test_match_all_months_es_and_en(lib, rule_set, month: str):
    es_word = ES_MONTH_FORMS[month]
    es_matches = match_line(_line(f"desde {es_word} espero"), 0, lib, rules=rule_set)
    assert any(month in m.categories for m in es_matches), f"Spanish {es_word} missed"

    en_matches = match_line(_line(EN_MONTH_LINES[month]), 0, lib, rules=rule_set)
    assert any(month in m.categories for m in en_matches), f"English {month} missed"


def test_match_spanish_variants_case_and_accents(lib, rule_set):
    for text, expected in (
        ("en setiembre vuelve", "september"),
        ("EN ENERO DESPIERTA", "january"),
        ("En Agósto brilla", "august"),
    ):
        matches = match_line(_line(text), 0, lib, rules=rule_set)
        assert any(expected in m.categories for m in matches), (text, matches)


@pytest.mark.parametrize(
    "text,forbidden",
    [
        ("Julia canta bajo la luna", "july"),
        ("La cultura maya brilla", "may"),
        ("El desmayo fue lento", "may"),
        ("We are marching home", "march"),
        ("They marched together", "march"),
        ("Soldiers marches onward", "march"),
        ("tears may fall tonight", "may"),
        ("May I stay a while", "may"),
        ("It may be too late", "may"),
        ("you may leave now", "may"),
        ("we march on forever", "march"),
        ("don Julio llegó ayer", "july"),
        ("me llamo Abril siempre", "april"),
    ],
)
def test_month_false_positive_and_substring_guards(lib, rule_set, text: str, forbidden: str):
    matches = match_line(_line(text), 0, lib, rules=rule_set)
    assert all(forbidden not in m.categories for m in matches), f"False positive {forbidden} in {text!r}: {matches}"


def test_month_guarded_positive_temporal_cues(lib, rule_set):
    for text, expected in (
        ("May rain on the glass", "may"),
        ("March wind on the glass", "march"),
        ("since may morning", "may"),
        ("march rain on the roof", "march"),
        ("Mayo trae color", "may"),
        ("En abril llega", "april"),
    ):
        matches = match_line(_line(text), 0, lib, rules=rule_set)
        assert any(expected in m.categories for m in matches), (text, matches)


def test_month_priority_and_emotion_coexistence(lib, rule_set):
    # Emotion categories (priority 6-9) take precedence over months (priority 5) under MAX_PER_LINE = 2,
    # while a single emotion + month coexist on the same line.
    coexist = match_line(_line("sadness in december", dur=2000), 0, lib, rules=rule_set)
    cats = {cat for m in coexist for cat in m.categories}
    assert len(coexist) <= FX.max_fx_per_line
    assert "sadness" in cats
    assert "december" in cats

    # Two higher-priority emotions fill MAX_PER_LINE before a priority-5 month.
    crowded = match_line(_line("love sadness pain in december", dur=2000), 0, lib, rules=rule_set)
    assert len(crowded) == FX.max_fx_per_line
    assert all(
        max(lib.categories[c].priority for c in m.categories) > lib.categories["december"].priority
        for m in crowded
    )


def test_month_signatures_distinct_and_waveforms_bounded(lib):
    sigs = [SIGNATURES[m] for m in MONTHS]
    identities = {(s.particle_kind, s.waveform, s.axis, s.sparkle_style, s.motif) for s in sigs}
    assert len(identities) == 12

    for wf in ("pendulum", "surge", "shimmer"):
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            val = eval_waveform(wf, t=t, freq_hz=1.0, letter_idx=0, seed_u=0.2)
            assert math.isfinite(val)
            assert -1.5 <= val <= 1.5


@pytest.mark.parametrize("month", MONTHS)
def test_month_particles_emit_within_bounds(lib, month: str):
    p = _compose_month(lib, month, dur_ms=1600, begin_ms=1000)
    xs = [i * 0.45 for i in range(6)]
    ws = [0.4 for _ in range(6)]

    pts_mid = particles(p, xs, ws, t=1.65, begin_s=1.0, end_s=2.6, reduce=False)
    assert len(pts_mid) > 0, f"Expected active particles for {month}"
    for pt in pts_mid:
        assert math.isfinite(pt.x_em) and math.isfinite(pt.y_em)
        assert 0.0 <= pt.alpha <= 1.0

    # Reduce motion disables dynamic particle emission cleanly.
    assert particles(p, xs, ws, t=1.65, begin_s=1.0, end_s=2.6, reduce=True) == []


def test_april_dew_petal_transition_and_equinox_streams(lib):
    april_p = _compose_month(lib, "april", dur_ms=1600, begin_ms=1000)
    xs = [i * 0.45 for i in range(5)]
    ws = [0.4 for _ in range(5)]

    early_kinds = {pt.kind for pt in particles(april_p, xs, ws, t=1.18, begin_s=1.0, end_s=2.6)}
    late_kinds = {pt.kind for pt in particles(april_p, xs, ws, t=2.10, begin_s=1.0, end_s=2.6)}
    assert "raindrop" in early_kinds
    assert "petal" in late_kinds

    sep_p = _compose_month(lib, "september", dur_ms=1600, begin_ms=1000)
    sep_kinds = {pt.kind for pt in particles(sep_p, xs, ws, t=1.70, begin_s=1.0, end_s=2.6)}
    assert "equinox" in sep_kinds
    assert "leaf" in sep_kinds


def test_duration_modes_syllable_onsets_and_rapid_transitions(lib):
    # Accent (< 0.25s) suppresses particles cleanly without popping.
    p_accent = _compose_month(lib, "january", dur_ms=180, begin_ms=1000)
    xs = [0.0, 0.4, 0.8]
    ws = [0.35, 0.35, 0.35]
    assert particles(p_accent, xs, ws, t=1.09, begin_s=1.0, end_s=1.18) == []

    # Held (> 0.70s) with syllable onsets drives staggered letter illumination cleanly.
    p_held = _compose_month(lib, "january", dur_ms=1400, begin_ms=1000)
    ls = letter_state(
        p_held,
        i=2,
        n=5,
        t=1.35,
        begin_s=1.0,
        end_s=2.4,
        onset=1.3,
    )
    assert math.isfinite(ls.alpha) and 0.0 <= ls.alpha <= 1.0
    assert math.isfinite(ls.dx) and math.isfinite(ls.dy)

    # Repeated month tokens in rapid succession match cleanly and stay bounded.
    lyr = Lyrics([
        Line(0, 400, syllables=[Syllable("e", 0, 130, False), Syllable("ne", 130, 260, False), Syllable("ro", 260, 400, True)]),
        _line("enero", start=420, dur=400),
        _line("febrero", start=840, dur=600),
    ])
    plan = build_plan(lyr, lib, track_id="months-rapid")
    assert all(plan.get(i, 0) is not None for i in range(3))


@pytest.mark.parametrize("month", MONTHS)
def test_render_month_snapshot_deterministic_and_non_blank(lib, month: str):
    es_word = ES_MONTH_FORMS[month]
    lyr = Lyrics([
        Line(
            0,
            1800,
            syllables=[
                Syllable("en ", 0, 350, True),
                Syllable(es_word, 350, 1800, True),
            ],
        )
    ])
    plan = build_plan(lyr, lib, track_id=f"month-{month}")
    cover = demo_cover()
    t_ms = 1100

    scene_a = Scene(lyr, cover, (270, 480), style=Style(), wordfx=plan)
    scene_b = Scene(lyr, cover, (270, 480), style=Style(), wordfx=plan)
    try:
        bytes_a = scene_a.snapshot(t_ms).tobytes()
        bytes_b = scene_b.snapshot(t_ms).tobytes()
        assert len(bytes_a) == 270 * 480 * 4
        assert bytes_a == bytes_b
    finally:
        scene_a.close()
        scene_b.close()


def test_months_performance_vs_baseline_benchmark(lib):
    month_lines = [_line(EN_MONTH_LINES[m], start=i * 1500, dur=1300) for i, m in enumerate(MONTHS)]
    baseline_words = (
        "cold winter night",
        "warm spring bloom",
        "bright summer heat",
        "quiet autumn leaf",
        "soft rain falling",
        "golden fire glow",
        "deep ocean tide",
        "sadness fades slow",
        "gentle hope rises",
        "sweet dream awake",
        "silver star shine",
        "peaceful mist drifts",
    )
    base_lines = [_line(txt, start=i * 1500, dur=1300) for i, txt in enumerate(baseline_words)]

    cover = demo_cover()
    month_lyrics = Lyrics(month_lines)
    base_lyrics = Lyrics(base_lines)
    month_plan = build_plan(month_lyrics, lib, track_id="bench-months")
    base_plan = build_plan(base_lyrics, lib, track_id="bench-base")

    def measure(lyr: Lyrics, plan) -> tuple[float, float]:
        scene = Scene(lyr, cover, (270, 480), style=Style(), wordfx=plan)
        try:
            for idx in (0, 3, 6, 9):
                scene.snapshot(idx * 1500 + 650)

            samples: list[float] = []
            for idx in range(12):
                for frac in (0.35, 0.70):
                    t_ms = idx * 1500 + int(1300 * frac)
                    t0 = time.perf_counter()
                    scene.snapshot(t_ms)
                    samples.append((time.perf_counter() - t0) * 1000.0)
            samples.sort()
            avg_ms = sum(samples) / len(samples)
            p95_ms = samples[int(0.95 * (len(samples) - 1))]
            return avg_ms, p95_ms
        finally:
            scene.close()

    base_avg, base_p95 = measure(base_lyrics, base_plan)
    month_avg, month_p95 = measure(month_lyrics, month_plan)

    assert month_avg > 0.0 and month_p95 > 0.0
    assert month_avg <= max(base_avg * 2.5, base_avg + 15.0)
