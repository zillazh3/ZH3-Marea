"""Synthetic audio checks for fixed-duration loop seams."""
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from marea.audio import local
from marea.audio.loop import loop_local, loop_pcm
from marea.sysutil import run_ffmpeg


@pytest.fixture
def sine():
    result = run_ffmpeg([
        "-v", "error", "-f", "lavfi", "-i", "sine=frequency=337:sample_rate=48000:duration=3",
        "-f", "f32le", "-ac", "2", "-",
    ])
    assert result.returncode == 0, result.stderr
    return np.frombuffer(result.stdout, np.float32).reshape(-1, 2).copy()


def test_loop_preserves_duration_interior_and_source_and_is_deterministic(sine):
    original = sine.copy()
    out = loop_local(sine, 255, 1234, 11, 160)
    again = loop_local(sine, 255, 1234, 11, 160)
    raw = local.segment(sine, 255, 1489, 11, fade_ms=0)
    assert out.shape == (round(1.234 * local.RATE), 2)
    assert out.dtype == np.float32 and out.flags.c_contiguous
    assert np.array_equal(out, again) and np.array_equal(sine, original)
    assert np.array_equal(out[4000:-4000], raw[4000:-4000])
    seam_jump = np.abs(out[0] - out[-1]).max()
    ordinary_step = np.abs(np.diff(out, axis=0)).max()
    assert seam_jump < 0.007 and seam_jump <= ordinary_step * 1.1
    assert np.abs(raw[0] - raw[-1]).max() > seam_jump * 4


@pytest.mark.parametrize("duration_ms", [0, 0.02, 1, 10, 50, 1000])
def test_loop_caps_fade_and_reflects_missing_handles(sine, duration_ms):
    length = round(duration_ms / 1000 * local.RATE)
    source = sine[:max(1, length)]
    out = loop_local(source, 0, duration_ms, 0, 500)
    assert out.shape == (length, 2) and np.isfinite(out).all()
    if length > 100:
        assert np.abs(out[0] - out[-1]).max() < 0.01


def test_zero_fade_keeps_exact_cut_and_native_capture_rate(sine):
    assert np.array_equal(loop_local(sine, 100, 500, 20, 0),
                          local.segment(sine, 100, 600, 20, fade_ms=0))
    captured = loop_pcm(sine, 44100, 1000, 5000, 150)
    assert captured.shape == (5000, 2) and captured.dtype == np.float32
    assert np.array_equal(captured[3400:3500], sine[4400:4500])


def test_empty_source_is_silence_and_bad_duration_is_rejected():
    assert not loop_local(np.empty((0, 2), np.float32), 0, 10, 0, 150).any()
    with pytest.raises(ValueError):
        loop_local(np.empty((0, 2), np.float32), 0, -10, 0, 150)


def test_range_outside_source_keeps_silence_instead_of_repeating_the_file(sine):
    assert not loop_local(sine, 5000, 1000, 0, 150).any()
    assert not loop_local(sine, -2000, 1000, 0, 150).any()


def test_wav_roundtrip_preserves_seam(sine, tmp_path):
    out = loop_local(sine, 255, 1234, 0, 160)
    path = tmp_path / "loop.wav"
    local.write_wav(path, out)
    decoded = local.decode(path)
    assert decoded.shape == out.shape
    assert np.abs(decoded[0] - decoded[-1]).max() < 0.007


def test_old_history_defaults_loop_off(tmp_path):
    from marea.export import history

    video = tmp_path / "clip.mp4"
    video.touch()
    path = tmp_path / "history.json"
    path.write_text(json.dumps([{"path": str(video)}]), encoding="utf-8")
    assert history.load(path)[0]["loop"] is False


def test_queue_skips_legacy_fades_and_keeps_snapped_job_duration(tmp_path):
    from marea.export.queue import AudioSpec, ExportQueue

    spec = SimpleNamespace(duration_ms=1000, audio_fade_ms=160)
    job = SimpleNamespace(loop=spec, start_ms=100, end_ms=1100, audio_path=None)
    item = SimpleNamespace(job=job, cancel_event=threading.Event(), wav=None,
                           audio=AudioSpec("local", 100, 1100,
                                           samples=np.full((local.RATE * 2, 2), 0.2, np.float32),
                                           fade_out_ms=1000))
    wav = ExportQueue._make_audio(None, item)
    try:
        out = local.decode(wav)
        assert out.shape == (local.RATE, 2) and out.min() > 0.19
        item._audio_duration_ms = 1300
        rendered = []
        queue = SimpleNamespace(_render=rendered.append)
        ExportQueue._audio_ready(queue, item, wav)
        assert job.end_ms == 1100 and rendered == [item]
    finally:
        wav.unlink(missing_ok=True)


def test_queue_cancel_before_audio_does_not_create_a_file():
    from marea.audio.session import RecordingCancelled
    from marea.export.queue import ExportQueue

    cancelled = threading.Event()
    cancelled.set()
    item = SimpleNamespace(job=SimpleNamespace(loop=None), audio=None, cancel_event=cancelled)
    with pytest.raises(RecordingCancelled):
        ExportQueue._make_audio(None, item)


def test_queue_records_effective_loop_state(monkeypatch, tmp_path):
    from marea.export import history
    from marea.export.queue import ExportQueue

    entries = []
    monkeypatch.setattr(history, "make_thumbnail", lambda path: None)
    monkeypatch.setattr(history, "add", entries.append)
    for loop in (None, SimpleNamespace()):
        item = SimpleNamespace(out_path=tmp_path / "clip.mp4", title="Title", artist="Artist",
                               job=SimpleNamespace(loop=loop))
        ExportQueue._record_history(None, item)
    assert [entry["loop"] for entry in entries] == [False, True]


def test_spotify_loop_keeps_capture_alignment_and_cancellation_cleanup(sine):
    from marea.audio.align import PositionSample
    from marea.audio.loopback import Recording
    from marea.audio.session import RecordingCancelled, record_song_range

    world = SimpleNamespace(now=100.0, begin=0.0, stopped=False, paused=False)

    class Client:
        def play(self, uri, position):
            world.begin, world.play = position / 1000, world.now

        def pause(self):
            world.paused = True

        def position_sample(self):
            now = world.now
            world.now += 0.01
            position = (world.begin + now + 0.005 - world.play) * 1000
            playback = SimpleNamespace(track=SimpleNamespace(id="track"), is_playing=True)
            return playback, PositionSample(position, now, world.now)

    class Recorder:
        def __init__(self, device):
            pass

        def start(self):
            world.t0 = world.now

        def stop(self):
            world.stopped = True
            start = round(world.begin * local.RATE)
            length = round((world.now - world.t0) * local.RATE)
            captured = np.zeros((length, 2), np.float32)
            available = sine[start:start + length]
            captured[:len(available)] = available
            return Recording(captured, local.RATE, world.t0)

    def sleep(seconds):
        world.now += seconds

    options = dict(recorder_factory=Recorder, clock=lambda: world.now, sleep=sleep, loop_fade_ms=160)
    track = SimpleNamespace(uri="spotify:track:track", id="track")
    out, rate = record_song_range(Client(), track, 1000, 2234, **options)
    reference = loop_local(sine, 1000, 1234, 0, 160)
    assert rate == local.RATE and np.array_equal(out, reference)
    assert world.paused and world.stopped
    world.paused = world.stopped = False
    with pytest.raises(RecordingCancelled):
        record_song_range(Client(), track, 1000, 2234, cancel=lambda: True, **options)
    assert world.paused and world.stopped
