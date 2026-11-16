"""Preview pacing and the F3 debug overlay (preview only: the export never draws it)."""
import pytest
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest

from marea.ui import preview as preview_mod


@pytest.fixture
def pv(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w.editor.preview


def test_timer_ticks_faster_than_one_sim_step(pv):
    # a tick as slow as one sim step (16 ms vs 16.67) periodically skips a step and the scroll advances in uneven strides
    assert pv._timer.interval() == preview_mod.TICK_MS < preview_mod.DT * 1000 / 1.5


def test_f3_toggles_the_overlay_and_it_paints(pv):
    keys = [s.key().toString() for s in pv.findChildren(QShortcut)]
    assert QKeySequence("F3").toString() in keys
    assert not pv._debug
    base = pv.grab().toImage()
    pv.toggle_debug()
    assert pv._debug
    lines = pv._debug_lines()
    assert lines[0].startswith("t ") and "item" in lines[1] and lines[2] == "lag n/a" and lines[3].startswith("frame")
    assert pv.grab().toImage() != base  # something was drawn over the preview
    pv.toggle_debug()
    assert not pv._debug


def test_lag_is_reported_while_playing(pv, qapp):
    pv.toggle_debug()
    pv.set_playing(True)
    QTest.qWait(120)
    qapp.processEvents()
    assert pv._debug_lines()[2].startswith("lag ") and pv._debug_lines()[2] != "lag n/a"
    pv.set_playing(False)
    assert pv._debug_lines()[2] == "lag n/a"
