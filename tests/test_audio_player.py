"""Unit tests for the real-time PreviewAudioPlayer and streaming resampling/clock math."""
import numpy as np
import pytest

from marea.audio.local import RATE
from marea.audio.player import PreviewAudioPlayer, render_audio_block


def test_render_audio_block_slices_pads_and_applies_offset_and_volume():
    # Ramp signal: sample i has value i / 1000
    samples = np.repeat((np.arange(RATE, dtype=np.float32) / 1000.0)[:, None], 2, axis=1)

    # Negative offset before start of file zero-pads left edge
    blk, nxt = render_audio_block(samples, 0.0, -10.0, 960, RATE, volume=0.5)
    assert blk.shape == (960, 2)
    assert nxt == pytest.approx(20.0)
    assert np.all(blk[:480] == 0.0)  # first 10 ms (480 samples) is silence
    assert blk[480, 0] == pytest.approx(0.0)
    assert blk[481, 0] == pytest.approx(0.001 * 0.5)

    # Muted block outputs silence
    muted, _ = render_audio_block(samples, 100.0, 0.0, 256, RATE, volume=0.8, muted=True)
    assert np.all(muted == 0.0)


def test_render_audio_block_resamples_for_non_48k_device():
    t = np.arange(RATE, dtype=np.float32) / RATE
    tone = np.repeat(np.sin(2 * np.pi * 440.0 * t)[:, None], 2, axis=1)
    blk, nxt = render_audio_block(tone, 100.0, 0.0, 441, 44100)
    assert blk.shape == (441, 2)
    assert nxt == pytest.approx(110.0)
    assert np.max(np.abs(blk)) > 0.9


class _FakeStream:
    def __init__(self, cb):
        self.cb = cb
        self.stopped = False
        self.closed = False

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakePA:
    def __init__(self, rate=48000):
        self.rate = rate
        self.streams = []
        self.terminated = False

    def get_default_output_device_info(self):
        return {"index": 0, "defaultSampleRate": self.rate}

    def open(self, **kw):
        s = _FakeStream(kw["stream_callback"])
        self.streams.append(s)
        return s

    def terminate(self):
        self.terminated = True


def test_player_shared_clock_seek_live_offset_and_loop(monkeypatch):
    now = [10.0]
    pa = _FakePA(rate=48000)
    player = PreviewAudioPlayer(clock=lambda: now[0], pa_factory=lambda: pa)
    samples = np.ones((RATE * 5, 2), np.float32) * 0.25
    player.set_samples(samples, offset_ms=0.0)

    assert player.play(1000.0, (1000.0, 2000.0), offset_ms=0.0)
    assert len(pa.streams) == 1

    # Simulate callback firing at t=10.000 with 40ms DAC latency
    raw, status = pa.streams[0].cb(None, 480, {"current_time": 5.0, "output_buffer_dac_time": 5.04}, 0)
    assert status == 0 and len(raw) == 480 * 2 * 4  # paContinue

    # Before DAC latency elapses, audible time stays anchored cleanly >= 1000ms
    now[0] = 10.020
    assert player.current_song_ms() == pytest.approx(1000.0)

    # After 40ms DAC latency + 10ms, audible time is 1010ms
    now[0] = 10.050
    assert player.current_song_ms() == pytest.approx(1010.0)

    # Live offset shift does NOT reopen the stream
    player.set_offset(250.0)
    assert len(pa.streams) == 1

    # Seek updates position without reopening stream
    player.seek(1500.0)
    assert len(pa.streams) == 1
    assert player.current_song_ms() == pytest.approx(1500.0)

    player.close()
    assert pa.terminated and pa.streams[0].closed


def test_periodic_preview_spans_the_export_seam_and_restarts_the_inspection_window():
    pa = _FakePA()
    player = PreviewAudioPlayer(pa_factory=lambda: pa)
    pcm = np.repeat(np.arange(80, dtype=np.float32)[:, None], 2, axis=1)
    start = 100.0
    step = 1000 / RATE
    player.set_samples(pcm, period_start_ms=start)
    assert player.play(start + 78 * step, (start + 50 * step, start + 110 * step), offset_ms=0.0)
    raw, _ = pa.streams[0].cb(None, 33, None, 0)
    np.testing.assert_array_equal(np.frombuffer(raw, np.float32)[::2], [78, 79, *range(30), 50])
    player.close()


def test_editor_transport_controls_waveform_seek_and_whole_second_readout(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    e = w.editor

    e._on_position(1353, 22353)
    assert e.time.text() == "0:01 / 0:22"

    # Attach fake local audio samples and verify live offset + mute + volume + waveform seek + stop
    samples = np.ones((RATE * 30, 2), np.float32) * 0.1
    e.project.audio_samples = samples
    e.preview.set_audio_samples(samples, e.project.audio_offset_ms)
    e.sl_volume.setValue(45)
    assert e.preview._audio.volume == pytest.approx(0.45)
    e.btn_mute.click()
    assert e.preview._audio.muted is True
    e.btn_mute.click()
    assert e.preview._audio.muted is False

    # Waveform seek updates preview position and playhead without clearing peak cache
    e.wave.set_data(samples, (5000.0, 12000.0), 0.0, [6000.0], (6000.0, 10000.0))
    _ = e.wave.peaks(50)
    cached_ref = e.wave._cache
    e.wave.seek_requested.emit(7500.0)
    qapp.processEvents()
    assert e.wave._cache is cached_ref
    assert e.preview.scene.engine.t * 1000 == pytest.approx(7500.0)
    assert e.wave._playhead_ms == pytest.approx(7500.0)

    # Stop rewinds to start of range
    e.btn_stop.click()
    qapp.processEvents()
    assert not e.preview.playing
    assert e.preview.scene.engine.t * 1000 == pytest.approx(e._clip_range()[0])
    w.close()
