"""Módulos puros del motor contra valores sacados ejecutando el prototipo original (no incluido en el repo)."""
import json
from pathlib import Path

import pytest

from marea.engine import curves, interlude
from marea.engine.layout import pos_y_policy
from marea.engine.springs import Spr, Spring

R = json.loads((Path(__file__).parent / "fixtures" / "engine_refs.json").read_text())
TOL = 1e-9


def close(a, b, tol=TOL):
    assert a == pytest.approx(b, abs=tol)


def test_beziers():
    easings = {"easeOut": curves.EASE_OUT, "lightingEasing": interlude.LIGHTING, "enterFadeEasing": interlude.ENTER_FADE,
               "exitP1": interlude.EXIT_P1, "exitP2": interlude.EXIT_P2, "exitFade": interlude.EXIT_FADE,
               "bezIn": curves.bezier(0.2, 0.4, 0.58, 1.0), "bezOut": curves.bezier(0.3, 0.0, 0.58, 1.0)}
    for name, f in easings.items():
        for x, want in zip(R["xs"], R[name]):
            close(f(x), want)


def test_splines():
    for name in ("SCALE", "YOFF", "GLOW", "L_SCALE", "L_YOFF"):
        for x, want in zip(R["xs"], R[name]):
            close(getattr(curves, name)(x), want)
    assert [curves.SCALE(-0.5), curves.SCALE(1.5)] == pytest.approx(R["splineOutside"])  # se acota a [0, 1]


@pytest.mark.parametrize("case", R["spr"])
def test_spr_analytic(case):
    s = Spr(case["p"], case["f"], case["d"])
    s.g, s.v = case["g"], case["v"]
    for dt, want in zip(case["dts"], case["out"]):
        close(s.step(dt), want)


@pytest.mark.parametrize("case", R["spring"])
def test_spring_substeps_delay_and_rest(case):
    s = Spring(0)
    s.k, s.c = case["k"], case["c"]
    now = 1000.0
    s.to(case["target"], case["delay"], now)
    for want in case["out"]:
        now += (1 / 60) * 1000
        close(s.step(1 / 60, now), want)
    assert s.p == case["target"]  # llegó al reposo y se quedó exacto


@pytest.mark.parametrize("case", R["policy"])
def test_pos_y_policy(case):
    k, c = pos_y_policy(case["seek"], case["inter"], case["iv"])
    close(k, case["out"]["k"])
    close(c, case["out"]["c"])


def test_breathing():
    for x, want in zip(R["xs"], R["breathing"]):
        close(interlude.breathing(x), want)


@pytest.mark.parametrize("case", R["interlude"], ids=lambda c: f"{c['total']}-{c['delayEnd']}")
def test_interlude_plan_and_samples(case):
    plan = interlude.plan_interlude(case["total"], case["delayEnd"])
    ref = case["plan"]
    if ref is None:
        assert plan is None
    else:
        assert (plan.mode, plan.body, plan.body_end) == (ref["mode"], ref["bodyMs"], ref["bodyEnd"])
        close(plan.dot3_target, ref["dot3Target"])
        if ref["mode"] == "breathe":
            assert (plan.seg, plan.dot3_dur) == (ref["seg"], ref["dot3Dur"])
            close(plan.period, ref["period"])
    for s in case["samples"]:
        op, sc, d, phase, f = interlude.interlude_snap(plan, s["el"])
        assert phase == s["phase"], s
        close(op, s["op"])
        close(sc, s["sc"])
        for a, b in zip(d, s["d"]):
            close(a, b)
        for a, b in zip(f, s["f"]):
            close(a, b)
