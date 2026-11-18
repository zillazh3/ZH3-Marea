"""Periodic cover motion, exact video decoding, and bounded source-loop seams."""
from types import SimpleNamespace
import subprocess

import numpy as np
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.model.background import BackgroundConfig, loop_source_samples
from marea.model.cinematic import CinematicConfig, CinematicTreatment, VideoResource
from marea.model.loop import LoopSpec
from marea.render.background import Background
from marea.render.cinematic import CinematicBackground, VideoBackground, probe_video
from marea.sysutil import NO_WINDOW, find_ffmpeg


def spec(duration_ms=6000.0, fade_ms=500.0):
    return LoopSpec(12500.0, 0, 0, round(duration_ms * 60 / 1000), video_fade_ms=fade_ms)


def pixels(background, phase_ms, loop):
    surface = skia.Surface(64, 96)
    surface.getCanvas().clear(skia.ColorTRANSPARENT)
    background.draw_loop(surface.getCanvas(), phase_ms, loop)
    return surface.makeImageSnapshot().toarray()


@pytest.mark.parametrize("speed", [0.0, 0.6, 1.0, 2.5])
def test_cover_loop_matches_virtual_endpoint_and_approaches_seam(speed):
    bg = Background(demo_cover(), 64, 96, speed=speed)
    loop = spec()
    first = pixels(bg, 0, loop)
    surface = skia.Surface(64, 96)
    bg.draw(surface.getCanvas(), loop.start_ms / 1000.0)
    assert np.array_equal(first, surface.makeImageSnapshot().toarray())
    assert np.array_equal(first, pixels(bg, loop.duration_ms, loop))
    assert np.array_equal(pixels(bg, 1234, loop), pixels(bg, 1234 + loop.duration_ms, loop))
    assert np.abs(first.astype(float) - pixels(bg, loop.duration_ms - 0.01, loop)).mean() < 0.2


@pytest.mark.parametrize("offset", [-1234.0, 0.0, 125.0, 1400.0])
@pytest.mark.parametrize("mode", ["repeat", "pingpong", "hold"])
def test_video_loop_samples_are_periodic_trimmed_and_normalized(offset, mode):
    resource = VideoResource("v", "unused.mp4", duration_ms=3000)
    cfg = BackgroundConfig(video=resource, trim_in_ms=500, trim_out_ms=2000, offset_ms=offset, loop=mode)
    loop = spec()
    for phase in (0.0, 100.0, 1234.0, 5999.99):
        samples = loop_source_samples(cfg, phase, loop)
        assert np.asarray(samples) == pytest.approx(np.asarray(loop_source_samples(cfg, phase + loop.duration_ms, loop)))
        assert sum(weight for _, weight in samples) == pytest.approx(1.0)
        assert all(500 <= source <= 2000 - resource.frame_duration_ms for source, _ in samples)


def test_short_video_window_uses_smooth_pingpong():
    cfg = BackgroundConfig(video=VideoResource("v", "unused.mp4", duration_ms=120))
    loop = spec(3000)
    values = [loop_source_samples(cfg, phase, loop) for phase in (0, 0.01, 1500, 2999.99, 3000)]
    assert all(len(value) == 1 for value in values)
    assert values[0] == values[-1]
    assert abs(values[0][0][0] - values[-2][0][0]) < 0.01


def test_repeat_blends_head_and_tail_and_offset_moves_source_phase():
    resource = VideoResource("v", "unused.mp4", duration_ms=2000)
    cfg = BackgroundConfig(video=resource)
    loop = spec(5000)
    blended = [loop_source_samples(cfg, phase, loop) for phase in range(0, 5000, 25)]
    assert any(len(samples) == 2 and 0.1 < samples[1][1] < 0.9 for samples in blended)
    before = loop_source_samples(cfg, 0, loop)[0][0]
    cfg.offset_ms = 100
    assert loop_source_samples(cfg, 0, loop)[0][0] == pytest.approx(before + 100)


def test_one_frame_trim_holds_only_available_frame():
    resource = VideoResource("v", "unused.mp4", duration_ms=2000)
    cfg = BackgroundConfig(video=resource, trim_in_ms=1000, trim_out_ms=1000 + resource.frame_duration_ms)
    assert loop_source_samples(cfg, 1234, spec()) == ((1000, 1.0),)


def test_testsrc_video_loop_preview_and_export_use_identical_frames(tmp_path):
    path = tmp_path / "loop.mp4"
    subprocess.run([find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=64x96:r=30:d=2", "-pix_fmt", "yuv420p", str(path)],
                   check=True, creationflags=NO_WINDOW)
    resource = probe_video(path)
    cfg = BackgroundConfig(source="video", video=resource, trim_in_ms=100, trim_out_ms=1900,
                           cover_tint=0.2, dim=0.1)
    loop = spec(5000)
    cover = Background(demo_cover(), 64, 96)
    preview = VideoBackground(cfg, 64, 96, cover, async_video=True)
    export = VideoBackground(cfg, 64, 96, cover)
    try:
        for phase in (0, 1234, 4800, 4999.99, 5000):
            assert np.array_equal(pixels(preview, phase, loop), pixels(export, phase, loop))
        assert np.array_equal(pixels(preview, 0, loop), pixels(preview, 5000, loop))
        first = pixels(preview, 0, loop).astype(float)
        seam = np.abs(first - pixels(preview, 5000 - 1000 / 60, loop)).mean()
        following = np.abs(first - pixels(preview, 1000 / 60, loop)).mean()
        assert seam <= max(3.0, following * 2)
    finally:
        preview.close()
        export.close()


def test_cinematic_loop_all_treatments_match_at_seam():
    resource = VideoResource("v", "unused.mp4", duration_ms=20000)
    cfg = CinematicConfig(enabled=True, primary_video_id="v", resources={"v": resource})
    cfg.add_interval(10000, 17000, 0, 7000, treatment=CinematicTreatment(grain=0.3))
    cinematic = CinematicBackground(cfg, 64, 96)
    image = demo_cover()
    cinematic._decoder_for = lambda _resource: SimpleNamespace(get_image_exact=lambda _ms: image)
    cover = Background(image, 64, 96)
    loop = spec()

    def draw(phase):
        surface = skia.Surface(64, 96)
        cinematic.draw_loop(surface.getCanvas(), cover, phase, loop, lyric_act=0.8)
        return surface.makeImageSnapshot().toarray()

    assert np.array_equal(draw(0), draw(loop.duration_ms))
    assert np.abs(draw(0).astype(float) - draw(loop.duration_ms - 0.01)).mean() < 0.2
    assert not np.array_equal(draw(0), draw(2000))
