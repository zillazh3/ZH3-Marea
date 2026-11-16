"""Nothing in the lyric engine may teleport: line scale, lines leaving the active range, and the scroll-jump logger."""
import logging

import pytest

from marea.demo_song import exact_lyrics
from marea.engine import engine as engine_mod
from marea.engine.engine import Engine, Params
from marea.engine.tuning import Tuning
from marea.model.lyrics import Line, Lyrics, Syllable

DT = 1 / 60


def measure(line, active):
    return 60.0 + (30.0 if active and line.bg else 0.0)


def rapid_lyrics():
    """Rap-like: 0.3 s lines every 0.35 s, so a line leaves the active neighbourhood while its springs are still moving."""
    out = []
    for i in range(8):
        b = 1000 + i * 350
        out.append(Line(b, b + 300, syllables=[Syllable("la", b, b + 120, True), Syllable("nota", b + 120, b + 300, True)]))
    return Lyrics(out)


def test_line_scale_eases_to_rest_instead_of_snapping():
    """The scale spring used to snap to rest once within 0.01 of its target: a 0.5 % jump (several px) on every line change."""
    eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=False))
    eng.warmup(8000)
    prev, worst = [L.s.p for L in eng.items], 0.0
    for _ in range(60 * 40):
        eng.advance(DT)
        cur = [L.s.p for L in eng.items]
        worst = max(worst, max(abs(a - b) for a, b in zip(prev, cur)))
        prev = cur
    assert worst < 0.0035  # the natural peak step is ~0.0027; the old snap was ~0.0046


def far_steps(monkeypatch, tail_settle_s):
    """Largest per-frame change (scale, y, glow alpha) of syllables that are out of the active neighbourhood in this frame or
    the previous one (the snap used to happen exactly on the frame a line left it)."""
    monkeypatch.setattr(engine_mod, "TUNING", Tuning(tail_settle_s=tail_settle_s))
    eng = Engine(rapid_lyrics(), measure, 900, params=Params(vib=False))
    eng.warmup(800)
    prev, worst = None, [0.0, 0.0, 0.0]
    for _ in range(int(3.6 * 60)):
        eng.advance(DT)
        cur = {}
        for L in eng.items:
            if L.kind == "line":
                far = L.index < eng.active - 1
                for wi, w in enumerate(L.words):
                    for pi, u in enumerate(w.parts):
                        cur[L.index, wi, pi] = (u.scale, u.y, u.glow_a, far)
        if prev:
            for k, vals in cur.items():
                if vals[3] or prev[k][3]:
                    for c in range(3):
                        worst[c] = max(worst[c], abs(vals[c] - prev[k][c]))
        prev = cur
    return worst


def test_lines_leaving_the_active_range_ease_out_instead_of_snapping(monkeypatch):
    scale, y, glow = far_steps(monkeypatch, tail_settle_s=1.5)
    assert scale < 0.002 and y < 0.002 and glow < 0.015


def test_without_tail_settling_the_same_fixture_does_snap(monkeypatch):
    """Guards the test above: it must be able to fail (this is the old behaviour)."""
    scale, y, glow = far_steps(monkeypatch, tail_settle_s=0.0)
    assert scale > 0.004 and y > 0.002 and glow > 0.015


def test_jump_logger_is_silent_in_normal_playback_and_reports_a_teleport(caplog):
    eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=False))
    eng.warmup(8000)
    with caplog.at_level(logging.WARNING, logger="marea.engine.engine"):
        for _ in range(60 * 40):
            eng.advance(DT)
        assert not [r for r in caplog.records if "scroll jump" in r.message]
        eng.items[3].y.p += 500.0  # force a teleport
        eng.advance(DT)
    hits = [r for r in caplog.records if "scroll jump" in r.message]
    assert len(hits) == 1 and "item=3" in hits[0].message


def test_seek_and_init_frames_are_not_reported_as_jumps(caplog):
    eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=False))
    with caplog.at_level(logging.WARNING, logger="marea.engine.engine"):
        eng.warmup(20000)
        eng.seek(5000)
        eng.advance(DT)
        eng.reconfigure()
        eng.advance(DT)
    assert not caplog.records


@pytest.mark.parametrize("reduce", [False, True])
def test_engine_stays_deterministic_with_tail_settling(reduce):
    runs = []
    for _ in range(2):
        eng = Engine(rapid_lyrics(), measure, 900, params=Params(reduce=reduce))
        eng.warmup(2500)
        for _ in range(120):
            eng.advance(DT)
        runs.append([(L.y.p, L.s.p, [(u.scale, u.y, u.glow_a) for w in L.words for u in w.parts]) for L in eng.items])
    assert runs[0] == runs[1]
