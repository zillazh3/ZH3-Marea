"""Non-blocking real-time WASAPI audio preview player with shared DAC-compensated playback clock."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

import numpy as np

from marea.audio.local import RATE


def render_audio_block(
    samples: np.ndarray | None,
    song_ms: float,
    offset_ms: float,
    frames: int,
    out_rate: int,
    *,
    volume: float = 1.0,
    muted: bool = False,
) -> tuple[np.ndarray, float]:
    """Slice and resample `frames` stereo float32 samples at `out_rate`, zero-padding out of bounds.

    Uses the exact `local.segment()` offset convention (`file_ms = song_ms + offset_ms`).
    Returns `(block_f32_Nx2, next_song_ms)`.
    """
    if frames <= 0 or out_rate <= 0:
        return np.zeros((0, 2), np.float32), song_ms
    next_ms = song_ms + (frames / out_rate) * 1000.0
    gain = 0.0 if muted else max(0.0, min(1.0, float(volume)))
    if samples is None or len(samples) == 0 or gain == 0.0:
        return np.zeros((frames, 2), np.float32), next_ms

    n = len(samples)
    src_start = (song_ms + offset_ms) / 1000.0 * RATE

    if out_rate == RATE:
        i0 = round(src_start)
        i1 = i0 + frames
        if i1 <= 0 or i0 >= n:
            return np.zeros((frames, 2), np.float32), next_ms
        out = np.zeros((frames, 2), np.float32)
        src_lo = max(0, i0)
        src_hi = min(n, i1)
        dst_lo = src_lo - i0
        dst_hi = dst_lo + (src_hi - src_lo)
        out[dst_lo:dst_hi] = samples[src_lo:src_hi]
        if gain != 1.0:
            out *= gain
        return out, next_ms

    step = RATE / float(out_rate)
    pos = src_start + np.arange(frames, dtype=np.float64) * step
    i0 = np.floor(pos).astype(np.int64)
    frac = (pos - i0).astype(np.float32)[:, None]

    valid0 = (i0 >= 0) & (i0 < n)
    i1 = i0 + 1
    valid1 = (i1 >= 0) & (i1 < n)

    if not (valid0.any() or valid1.any()):
        return np.zeros((frames, 2), np.float32), next_ms

    s0 = np.zeros((frames, 2), np.float32)
    s1 = np.zeros((frames, 2), np.float32)
    if valid0.any():
        s0[valid0] = samples[i0[valid0]]
    if valid1.any():
        s1[valid1] = samples[i1[valid1]]

    out = ((1.0 - frac) * s0 + frac * s1).astype(np.float32, copy=False)
    if gain != 1.0:
        out *= gain
    return out, next_ms


@dataclass
class _State:
    samples: np.ndarray | None = None
    period_start_ms: float | None = None
    playing: bool = False
    cursor_song_ms: float = 0.0
    range_ms: tuple[float, float] = (0.0, 1.0)
    offset_ms: float = 0.0
    volume: float = 1.0
    muted: bool = False
    loop: bool = True
    anchor_song_ms: float = 0.0
    anchor_perf: float = 0.0
    last_good_ms: float = 0.0


class PreviewAudioPlayer:
    """Thread-safe streaming audio player providing a shared master clock for Editor preview."""

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter, pa_factory=None):
        self._clock = clock
        self._pa_factory = pa_factory
        self._lock = threading.Lock()
        self._state = _State()
        self._pa = None
        self._stream = None
        self._rate = RATE
        self._device_error = ""

    @property
    def has_audio(self) -> bool:
        with self._lock:
            return self._state.samples is not None and len(self._state.samples) > 0

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._state.playing and self._stream is not None

    @property
    def device_error(self) -> str:
        return self._device_error

    @property
    def volume(self) -> float:
        with self._lock:
            return self._state.volume

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._state.muted

    def set_samples(self, samples: np.ndarray | None, offset_ms: float | None = None, *, period_start_ms: float | None = None):
        with self._lock:
            self._state.samples = samples
            self._state.period_start_ms = period_start_ms
            if offset_ms is not None:
                self._state.offset_ms = float(offset_ms)
        if samples is None and self._stream is not None:
            self.pause()

    def set_offset(self, offset_ms: float):
        """Update lyric-audio offset live without stopping or reopening the stream."""
        with self._lock:
            self._state.offset_ms = float(offset_ms)

    def set_range(self, range_ms: tuple[float, float]):
        with self._lock:
            self._state.range_ms = (float(range_ms[0]), max(float(range_ms[0]) + 1.0, float(range_ms[1])))

    def set_volume(self, volume: float):
        with self._lock:
            self._state.volume = max(0.0, min(1.0, float(volume)))

    def set_muted(self, muted: bool):
        with self._lock:
            self._state.muted = bool(muted)

    def seek(self, song_ms: float):
        """Jump audio cursor cleanly without tearing down the active stream."""
        now = self._clock()
        with self._lock:
            lo, hi = self._state.range_ms
            clamped = max(lo, min(hi, float(song_ms)))
            self._state.cursor_song_ms = clamped
            self._state.anchor_song_ms = clamped
            self._state.anchor_perf = now
            self._state.last_good_ms = clamped

    def play(self, song_ms: float, range_ms: tuple[float, float], offset_ms: float) -> bool:
        """Start streaming from `song_ms` within `range_ms`. Returns False if silent/unavailable."""
        self.set_range(range_ms)
        self.set_offset(offset_ms)
        if not self.has_audio:
            self._close_stream()
            return False
        self.seek(song_ms)
        if self._stream is None:
            if not self._open_stream():
                return False
        with self._lock:
            self._state.playing = True
            self._state.anchor_perf = self._clock()
            self._state.anchor_song_ms = self._state.cursor_song_ms
            self._state.last_good_ms = self._state.cursor_song_ms
        return True

    def pause(self):
        with self._lock:
            if self._state.playing:
                self._state.cursor_song_ms = self._unlocked_song_ms()
            self._state.playing = False
        self._close_stream()

    def stop(self):
        with self._lock:
            lo = self._state.range_ms[0]
            self._state.playing = False
            self._state.cursor_song_ms = lo
            self._state.anchor_song_ms = lo
            self._state.last_good_ms = lo
        self._close_stream()

    def current_song_ms(self) -> float | None:
        """Return the estimated audible song position in ms, or None when audio is not active."""
        with self._lock:
            if not self._state.playing or self._stream is None or self._state.samples is None:
                return None
            return self._unlocked_song_ms()

    def _unlocked_song_ms(self) -> float:
        s = self._state
        lo, hi = s.range_ms
        if s.period_start_ms is not None:
            elapsed = max(0.0, self._clock() - s.anchor_perf) * 1000.0
            return lo + (s.anchor_song_ms - lo + elapsed) % (hi - lo)
        est = s.anchor_song_ms + (self._clock() - s.anchor_perf) * 1000.0
        est = max(s.last_good_ms, est)
        est = max(lo, min(hi, est))
        s.last_good_ms = est
        return est

    def _render_callback_frames(self, frames: int, time_info: dict | None) -> tuple[bytes, bool]:
        now = self._clock()
        with self._lock:
            s = self._state
            if not s.playing or s.samples is None:
                return np.zeros((frames, 2), np.float32).tobytes(), False
            lo, hi = s.range_ms
            cur = s.cursor_song_ms
            if cur >= hi:
                if s.loop:
                    cur = lo
                    s.last_good_ms = lo
                else:
                    s.playing = False
                    return np.zeros((frames, 2), np.float32).tobytes(), False
            if s.period_start_ms is not None:
                step = RATE / self._rate
                window = max(1, round((hi - lo) * RATE / 1000))
                beginning = round((lo - s.period_start_ms) * RATE / 1000)
                cursor = (cur - lo) * RATE / 1000
                if abs(cursor - round(cursor)) < 1e-6:
                    cursor = round(cursor)
                positions = beginning + (np.arange(frames) * step + cursor) % window
                if self._rate == RATE:
                    block = s.samples[np.rint(positions).astype(np.int64) % len(s.samples)]
                else:
                    floor = np.floor(positions)
                    indices = floor.astype(np.int64)
                    fraction = (positions - floor).astype(np.float32)[:, None]
                    block = ((1 - fraction) * s.samples[indices % len(s.samples)]
                             + fraction * s.samples[(indices + 1) % len(s.samples)])
                block *= 0 if s.muted else s.volume
                next_ms = lo + (cursor + frames * step) % window * 1000 / RATE
            else:
                block, next_ms = render_audio_block(
                    s.samples, cur, s.offset_ms, frames, self._rate, volume=s.volume, muted=s.muted,
                )
            dac_lat = 0.0
            if time_info:
                try:
                    lat = float(time_info.get("output_buffer_dac_time", 0.0)) - float(time_info.get("current_time", 0.0))
                    if 0.0 <= lat <= 0.25:
                        dac_lat = lat
                except (TypeError, ValueError):
                    dac_lat = 0.0
            s.anchor_song_ms = cur
            s.anchor_perf = now + dac_lat
            if next_ms >= hi and s.loop:
                s.cursor_song_ms = lo + (next_ms - hi)
            else:
                s.cursor_song_ms = min(hi, next_ms)
            return block.tobytes(), True

    def _open_stream(self) -> bool:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            self._device_error = str(e)
            return False
        try:
            if self._pa is None:
                self._pa = self._pa_factory() if self._pa_factory else pyaudio.PyAudio()
            info = self._pa.get_default_output_device_info()
            rate = max(8000, int(info.get("defaultSampleRate", RATE)))
            self._rate = rate
            pa_mod = pyaudio

            def _callback(_in_data, frame_count, time_info, _status):
                data, keep_going = self._render_callback_frames(frame_count, time_info)
                return data, pa_mod.paContinue if keep_going else pa_mod.paComplete

            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=2,
                rate=rate,
                output=True,
                output_device_index=info["index"],
                frames_per_buffer=1024,
                stream_callback=_callback,
            )
            self._device_error = ""
            return True
        except OSError as e:
            self._device_error = str(e)
            self._close_stream()
            return False

    def _close_stream(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except OSError:
                pass

    def close(self):
        self.stop()
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except OSError:
                pass
