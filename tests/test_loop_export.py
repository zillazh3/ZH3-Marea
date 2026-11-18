from dataclasses import replace

import pytest
import numpy as np

from marea.export.job import render_frames
from marea.model.loop import LoopSpec, suggest_bar_end
from test_export import job


@pytest.mark.parametrize(("fps", "ticks"), [(30, 62), (60, 61)])
def test_loop_job_rounds_up_on_the_output_grid(tmp_path, fps, ticks):
    spec = LoopSpec.for_range(12500, 13501, 1, 3, fps)
    export = replace(job(tmp_path), fps=fps, loop=spec)
    assert spec.ticks == ticks
    assert export.frames == ticks * fps // 60
    assert export.start_ms == spec.start_ms
    assert export.end_ms == spec.end_ms
    assert (spec.first, spec.last) == (1, 3)


def test_bar_suggestion_preserves_first_and_last_syllables():
    beats = list(range(250, 5000, 500))
    assert suggest_bar_end(250, 2140, 2170, beats, 500, 0, 5000) == 2250
    assert suggest_bar_end(250, 2280, 2290, beats, 500, 0, 5000) is None
    assert suggest_bar_end(250, 2140, 2170, [], 500, 0, 5000) is None
    assert suggest_bar_end(150, 2040, 2070, beats, 500, 100, 5000) == 2150


@pytest.mark.parametrize("fps", [30, 60])
def test_loop_render_cadence_and_virtual_endpoint(tmp_path, fps):
    spec = LoopSpec.for_range(6000, 9200, 0, 0, fps)
    export = replace(job(tmp_path, marker="flash"), fps=fps, loop=spec)
    frames = list(render_frames(export, include_loop_endpoint=True))
    assert len(frames) == export.frames + 1
    assert not (frames[0] == 255).all()
    assert np.abs(frames[-1].astype(np.int16) - frames[0]).mean() < 1
