import itertools
import pickle
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export.job import ExportJob, render_frames
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import library
from marea.wordfx.palette import dominant_colors
from marea.wordfx.plan import build_plan


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def line_of(text: str, start=1000, per_word=400) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


def test_dominant_colors_are_rgb_triples():
    cols = dominant_colors(demo_cover())
    assert 1 <= len(cols) <= 3 and all(len(c) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in c) for c in cols)


def test_plan_composes_every_match_with_bleed_between_neighbors(lib):
    plan = build_plan(Lyrics([line_of("Fue tanto el tiempo perdido")]), lib, track_id="t1")
    assert sorted(plan.words) == [(0, 3), (0, 4)]
    tiempo, perdido = plan.get(0, 3), plan.get(0, 4)
    assert tiempo.params.bleed > 0 and perdido.params.bleed > 0  # son vecinas a 1 palabra
    assert tiempo.params.color_to != tiempo.params.color_from  # «perdido» contagia a «tiempo»


def test_repeated_line_looks_identical(lib):
    plan = build_plan(Lyrics([line_of("para siempre perder", start=0), line_of("para siempre perder", start=5000)]), lib, track_id="t1")
    first, second = plan.get(0, 2), plan.get(1, 2)
    assert (first.params.repeat_n, second.params.repeat_n) == (0, 1)
    assert first.params.seed == second.params.seed
    same = lambda p: replace(p, repeat_n=0, vanish_s=0.0, bleed_donor_s=0.0, bleed_donor_end_s=0.0, handoff_overlap_s=0.0)  # timestamps, not look
    assert same(first.params) == same(second.params)


def test_plan_is_deterministic_and_picklable(lib):
    a = build_plan(exact_lyrics(), lib, track_id="t1")
    b = build_plan(exact_lyrics(), lib, track_id="t1")
    assert a.words == b.words
    assert pickle.loads(pickle.dumps(a)).words == a.words
    assert build_plan(exact_lyrics(), lib, track_id="otra").words != a.words  # la semilla depende de la pista


def test_rms_is_neutral_without_audio_and_sampled_with_it(lib):
    assert build_plan(exact_lyrics(), lib).rms_at(3.0) == 0.5
    analysis = SimpleNamespace(rms=np.array([0.1, 0.9], np.float32), rate=1.0, beat_period_ms=500.0, beats_ms=[])
    plan = build_plan(exact_lyrics(), lib, analysis=analysis)
    assert (plan.rms_at(0.2), plan.rms_at(1.5), plan.rms_at(99)) == (pytest.approx(0.1), pytest.approx(0.9), pytest.approx(0.9))


def test_export_frames_are_identical_between_runs_and_differ_from_plain(lib):
    lyrics = exact_lyrics()
    plan = build_plan(lyrics, lib, track_id="t1")

    def frames(wordfx):
        job = ExportJob(lyrics, bytes(demo_cover().encodeToData()), None, 22000, 23500, size=(270, 480), prefer_gpu=False,
                        wordfx=wordfx)
        gen = render_frames(job)
        out = [f.copy() for f in itertools.islice(gen, 0, 90, 30)]  # 3 frames repartidos en 1.5 s
        gen.close()
        return out

    a, b, plain = frames(plan), frames(plan), frames(None)
    assert all(np.array_equal(x, y) for x, y in zip(a, b))
    assert any(not np.array_equal(x, y) for x, y in zip(a, plain))
    pickle.dumps(ExportJob(lyrics, b"", None, 0, 1, wordfx=plan))  # el job viaja al proceso de export


def test_eroding_words_vanish_when_the_next_verse_starts(lib):
    from marea.model.lyrics import Lyrics
    ly = Lyrics([line_of("fue perdido", 1000), line_of("otra cosa", 6000)])
    plan = build_plan(ly, lib, track_id="t", overrides=[])
    p = plan.get(0, 1).params
    assert p.erode > 0 and p.vanish_s == pytest.approx(6.0)
    assert Lyrics and build_plan(Lyrics([line_of("fue perdido", 1000)]), lib, track_id="t", overrides=[]).get(0, 1).params.vanish_s > 0  # último verso: su fin
