"""Guards for the editor freezes: preview video decoding, loop seeks and scrubbing must stay cheap."""
import subprocess
import threading
from collections import Counter

import numpy as np
import pytest

import marea.render.cinematic as cin
from marea.engine.engine import Engine
from marea.engine.loop import LoopEngine
from marea.model.cinematic import VideoResource
from marea.render.scene import LOOP_SNAP_TICKS

W, H = 8, 4


def fake_decoder(monkeypatch, async_decode):
    """VideoDecoder whose ffmpeg run fabricates frames (pixel value = frame index % 256) and records each call."""
    calls = []

    def run(self, t0_s, count):
        k0 = round(t0_s * self.fps_num / self.fps_den)
        calls.append((k0 // cin.CHUNK_FRAMES, threading.current_thread() is threading.main_thread()))
        return b"".join(bytes([(k0 + i) % 256]) * (W * H * 4) for i in range(count))

    monkeypatch.setattr(cin.VideoDecoder, "_run_ffmpeg_raw", run)
    res = VideoResource(id="v", path="unused.mp4", duration_ms=10_000, fps_num=30, fps_den=1)
    return cin.VideoDecoder(res, (W, H), async_decode=async_decode), calls


def frame_value(img):
    return int(img.toarray()[0, 0, 0])


def test_sequential_preview_playback_decodes_each_chunk_once(monkeypatch):
    dec, calls = fake_decoder(monkeypatch, async_decode=True)
    try:
        frames = 5 * cin.CHUNK_FRAMES
        dec.get_image(0.0)
        dec.wait_idle()
        for idx in range(frames):
            img = dec.get_image(idx * 1000 / 30 + 1)
            assert img is not None and frame_value(img) == idx % 256
            dec.wait_idle()
        counts = Counter(chunk for chunk, _ in calls)
        assert all(n == 1 for n in counts.values()), counts  # a frame-level LRU evicted upcoming frames and re-decoded
    finally:
        dec.close()


def test_loop_exact_frames_never_decode_on_the_ui_thread(monkeypatch):
    dec, calls = fake_decoder(monkeypatch, async_decode=True)
    try:
        for idx in (5, 200, 6, 201, 47):  # tail/head alternation like a loop seam
            img = dec.get_image_exact(idx * 1000 / 30 + 1)
            assert frame_value(img) == idx % 256  # exact, never a nearby substitute
        assert calls and not any(on_main for _, on_main in calls)
    finally:
        dec.close()


def test_preview_ffmpeg_is_thread_capped_and_low_priority_but_export_is_not(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    seen = []

    class FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            seen.append((cmd, kw.get("creationflags", 0)))

        def communicate(self, timeout=None):
            return b"", b""

    monkeypatch.setattr(cin, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cin.subprocess, "Popen", FakeProc)
    res = VideoResource(id="v", path=str(video), duration_ms=1000, fps_num=30, fps_den=1)
    for async_decode in (True, False):
        dec = cin.VideoDecoder(res, (W, H), async_decode=async_decode)
        dec._run_ffmpeg_raw(0.0, 1)
        dec.close()
    (preview_cmd, preview_flags), (export_cmd, export_flags) = seen
    assert "-threads" in preview_cmd and "-threads" not in export_cmd
    low = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    assert preview_flags & low == low and not (export_flags & low)


def test_backward_loop_seek_resimulates_from_a_snapshot_and_stays_exact(monkeypatch):
    from tests.test_loop_scene import fixture, pixels

    scene, spec = fixture()
    sequential = pixels(scene, 95)
    pixels(scene, spec.ticks)
    frames = []
    monkeypatch.setattr(LoopEngine, "_frame", lambda self, dt: (frames.append(dt), Engine._frame(self, dt))[1])
    again = pixels(scene, 95)
    assert len(frames) <= LOOP_SNAP_TICKS  # was a rebuild + 721 preroll frames + 95 on every wrap / backward scrub
    assert np.array_equal(again, sequential)


def test_restyle_drops_loop_snapshots_so_seeks_stay_exact():
    from tests.test_loop_scene import fixture, pixels

    scene, spec = fixture()
    pixels(scene, spec.ticks)
    scene.reconfigure()
    pixels(scene, 10)  # backward: rebuilds from scratch instead of restoring a pre-restyle snapshot
    fresh, _ = fixture()
    assert np.array_equal(pixels(scene, 95), pixels(fresh, 95))


@pytest.fixture
def win(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w


def test_scrub_drag_uses_short_warmup_and_release_settles_fully(win, monkeypatch):
    from marea.ui.preview import SCRUB_PREROLL_MS

    e = win.editor
    engine = e.preview.scene.engine
    prerolls = []
    original = engine.warmup
    monkeypatch.setattr(engine, "warmup", lambda t_ms, **kw: (prerolls.append(kw.get("preroll_ms", 3000)), original(t_ms, **kw)))
    e.scrub.setSliderDown(True)
    e._scrubbing = True
    e.scrub.setValue(e.scrub.maximum() // 2)
    e._flush_scrub()
    e.scrub.setSliderDown(False)
    e._on_scrub_released()
    assert prerolls[0] == SCRUB_PREROLL_MS and prerolls[-1] == 3000
