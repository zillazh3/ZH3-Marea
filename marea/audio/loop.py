"""Fixed-duration PCM loops with a crossfade centered on the wrap point."""
import math

import numpy as np

from marea.audio import local
from marea.audio.align import slice_padded


def _reflected_window(samples: np.ndarray, start: int, length: int) -> np.ndarray:
    if not len(samples):
        return np.zeros((length, samples.shape[1]), np.float32)
    indices = np.arange(start, start + length, dtype=np.int64)
    if len(samples) == 1:
        indices[:] = 0
    else:
        period = 2 * (len(samples) - 1)
        indices %= period
        indices = np.minimum(indices, period - indices)
    return samples[indices]


def loop_pcm(samples: np.ndarray, rate: int, start_sample: int, n_samples: int,
             fade_ms: float) -> np.ndarray:
    """Keep the exact cut and mix actual neighboring samples across its seam.

    Missing source handles reflect the selected cut. Only the seam region
    changes, so interior attacks retain their original lyric timing.
    """
    if rate <= 0 or n_samples < 0 or not math.isfinite(fade_ms) or fade_ms < 0:
        raise ValueError("Invalid loop audio duration, rate, or fade")
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 2:
        raise ValueError("Loop audio must have sample and channel dimensions")
    out = slice_padded(samples, rate, start_sample, n_samples, fade_ms=0)
    half = min(round(fade_ms * rate / 2000), n_samples // 4)
    if half:
        # End handles and start handles refer to the same relative seam time.
        tail = _reflected_window(out, n_samples - half, 2 * half)
        head = _reflected_window(out, -half, 2 * half)
        for window, start, dest in ((tail, start_sample + n_samples, half),
                                    (head, start_sample - half, 0)):
            lo, hi = max(0, start), min(len(samples), start + half)
            if hi > lo:
                window[dest + lo - start:dest + hi - start] = samples[lo:hi]
        phase = np.linspace(0.0, 1.0, 2 * half, dtype=np.float32)
        phase = (phase * phase * (3.0 - 2.0 * phase)) * (np.pi / 2)
        mixed = tail * np.cos(phase)[:, None] + head * np.sin(phase)[:, None]
        out[-half:], out[:half] = mixed[:half], mixed[half:]
    return np.ascontiguousarray(out, dtype=np.float32)


def loop_local(samples: np.ndarray, start_ms: float, duration_ms: float,
               offset_ms: float = 0.0, fade_ms: float = 150.0) -> np.ndarray:
    """Prepare local 48 kHz PCM identically for preview and export."""
    if not all(math.isfinite(v) for v in (start_ms, duration_ms, offset_ms)) or duration_ms < 0:
        raise ValueError("Invalid loop audio range")
    return loop_pcm(samples, local.RATE, round((start_ms + offset_ms) * local.RATE / 1000),
                    round(duration_ms * local.RATE / 1000), fade_ms)
