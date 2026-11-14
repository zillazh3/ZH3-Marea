"""Cached acoustic envelope analysis for cinematic suggestions and wordfx audio features."""
from __future__ import annotations

import math
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from marea.audio import align


@dataclass
class AudioAnalysis:
    """Acoustic curves at ~100 Hz for musical suggestions without recomputing FFT/RMS."""
    rate: float
    duration_ms: float
    rms: np.ndarray
    onset: np.ndarray
    contrast: np.ndarray
    pauses_ms: list[tuple[float, float]]
    accents_ms: list[tuple[float, float]]
    macro_contrast: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    tempo_bpm: float = 110.0
    beat_period_ms: float = 545.5
    beats_ms: list[float] = field(default_factory=list)


_ANALYSIS_CACHE: OrderedDict[tuple[int, tuple, int], AudioAnalysis] = OrderedDict()
_ANALYSIS_REFS: dict[tuple[int, tuple, int], weakref.ref] = {}
_ANALYSIS_LOCK = threading.Lock()
_ANALYSIS_CACHE_MAX = 4
_analysis_computations = 0  # observable in tests to verify cache hits


def _evict_key(key: tuple[int, tuple, int]) -> None:
    with _ANALYSIS_LOCK:
        _ANALYSIS_CACHE.pop(key, None)
        _ANALYSIS_REFS.pop(key, None)


def _moving_mean(x: np.ndarray, w: int) -> np.ndarray:
    if len(x) == 0 or w <= 1:
        return x.astype(np.float32, copy=True)
    w = min(w, len(x))
    csum = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    core = ((csum[w:] - csum[:-w]) / w).astype(np.float32)
    pad_l = w // 2
    pad_r = len(x) - len(core) - pad_l
    return np.concatenate([np.full(pad_l, core[0], np.float32), core, np.full(pad_r, core[-1], np.float32)])


def _estimate_tempo_and_beats(novelty: np.ndarray, env_rate: float) -> tuple[float, float, list[float]]:
    """Estimate tempo (BPM), beat period (ms), and beat grid via onset autocorrelation."""
    n = len(novelty)
    if n < round(2.2 * env_rate) or float(novelty.max()) < 0.14:
        return 110.0, 545.5, []

    max_bins = round(90.0 * env_rate)
    if n > max_bins:
        i0 = (n - max_bins) // 2
        seg = novelty[i0:i0 + max_bins]
    else:
        seg = novelty
    centered = (seg - float(seg.mean())).astype(np.float32)
    if float(np.dot(centered, centered)) <= 1e-6:
        return 110.0, 545.5, []

    lag_lo = max(4, round(env_rate * 60.0 / 185.0))
    lag_hi = min(len(centered) // 2, round(env_rate * 60.0 / 62.0))
    best_lag = max(lag_lo, round(env_rate * 60.0 / 110.0))
    best_score = -1e9
    for lag in range(lag_lo, lag_hi + 1):
        ac = float(np.dot(centered[:-lag], centered[lag:])) / max(1, len(centered) - lag)
        bpm = 60.0 * env_rate / lag
        prior = math.exp(-0.5 * ((bpm - 114.0) / 58.0) ** 2)
        score = ac * (0.65 + 0.35 * prior)
        if score > best_score:
            best_score = score
            best_lag = lag

    beat_period_ms = best_lag / env_rate * 1000.0
    tempo_bpm = 60000.0 / max(1.0, beat_period_ms)

    min_beat_sep = max(3, round(0.62 * best_lag))
    beat_thr = max(0.11, float(np.percentile(novelty, 74)))
    beats_ms: list[float] = []
    last_b = -min_beat_sep
    for idx in range(1, n - 1):
        v = float(novelty[idx])
        if v >= beat_thr and v >= novelty[idx - 1] and v >= novelty[idx + 1]:
            if idx - last_b >= min_beat_sep:
                beats_ms.append(idx / env_rate * 1000.0)
                last_b = idx
            elif beats_ms and v > float(novelty[last_b]):
                beats_ms[-1] = idx / env_rate * 1000.0
                last_b = idx
    return round(tempo_bpm, 2), round(beat_period_ms, 2), beats_ms


def _cache_lookup_locked(key: tuple[int, tuple, int], samples: np.ndarray) -> AudioAnalysis | None:
    cached = _ANALYSIS_CACHE.get(key)
    if cached is None:
        return None
    ref = _ANALYSIS_REFS.get(key)
    if ref is not None and ref() is not samples:
        _ANALYSIS_CACHE.pop(key, None)
        _ANALYSIS_REFS.pop(key, None)
        return None
    _ANALYSIS_CACHE.move_to_end(key)
    return cached


def _cache_store_locked(key: tuple[int, tuple, int], samples: np.ndarray, res: AudioAnalysis) -> None:
    try:
        ref = weakref.ref(samples, lambda _r, k=key: _evict_key(k))
    except TypeError:
        return
    _ANALYSIS_CACHE[key] = res
    _ANALYSIS_REFS[key] = ref
    _ANALYSIS_CACHE.move_to_end(key)
    while len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_MAX:
        old_key, _ = _ANALYSIS_CACHE.popitem(last=False)
        _ANALYSIS_REFS.pop(old_key, None)


def cached_audio_analysis(samples: np.ndarray, sample_rate: int = 48000) -> AudioAnalysis | None:
    """Return cached `AudioAnalysis` if already computed for `samples`, without blocking."""
    key = (id(samples), tuple(samples.shape), int(sample_rate))
    with _ANALYSIS_LOCK:
        return _cache_lookup_locked(key, samples)


def analyze_audio(samples: np.ndarray, sample_rate: int = 48000) -> AudioAnalysis:
    """Extract RMS, tempo/beats, section contrast, pauses, and accents using `align.onset_envelope` with LRU cache."""
    global _analysis_computations
    key = (id(samples), tuple(samples.shape), int(sample_rate))
    with _ANALYSIS_LOCK:
        cached = _cache_lookup_locked(key, samples)
        if cached is not None:
            return cached

    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    mono = np.asarray(mono, dtype=np.float32)
    duration_ms = len(mono) / max(1, sample_rate) * 1000.0
    hop = max(1, round(sample_rate / 100))
    env_rate = sample_rate / hop
    if len(mono) < hop * 4:
        empty = np.zeros(0, np.float32)
        res = AudioAnalysis(env_rate, duration_ms, empty, empty, empty, [], [], empty)
        with _ANALYSIS_LOCK:
            existing = _cache_lookup_locked(key, samples)
            if existing is not None:
                return existing
            _analysis_computations += 1
            _cache_store_locked(key, samples, res)
        return res

    win = 2 * hop
    n_rms = 1 + max(0, (len(mono) - win) // hop)
    sq = mono * mono
    csum = np.concatenate([[0.0], np.cumsum(sq, dtype=np.float64)])
    starts = np.arange(n_rms) * hop
    rms = np.sqrt(np.maximum(0.0, (csum[starts + win] - csum[starts]) / win)).astype(np.float32)

    onset, onset_rate = align.onset_envelope(mono, sample_rate)
    n = min(len(rms), len(onset)) if len(onset) else len(rms)
    rms = rms[:n]
    onset = onset[:n] if len(onset) else np.zeros(n, np.float32)
    env_rate = onset_rate if len(onset) else env_rate

    p95_rms = float(np.percentile(rms, 95)) if n else 0.0
    rms_norm = rms / max(p95_rms, 1e-6) if p95_rms > 1e-6 else np.zeros_like(rms)
    rms_span = float(np.percentile(rms_norm, 98) - np.percentile(rms_norm, 5)) if n else 0.0
    p95_on = float(np.percentile(onset, 95)) if n else 0.0
    on_span = float(np.percentile(onset, 99) - np.percentile(onset, 50)) if n else 0.0
    has_attacks = rms_span >= 0.05 and p95_on > 0.05 and on_span > 0.15
    onset_norm = onset / max(p95_on, 0.25) if has_attacks else np.zeros_like(rms)

    smooth_rms = _moving_mean(rms_norm, max(3, round(0.35 * env_rate)))
    contrast = _section_contrast(smooth_rms, env_rate, 1.6)
    macro_contrast = _section_contrast(smooth_rms, env_rate, 4.5)
    pauses_ms = _find_pauses(smooth_rms, env_rate)

    d_rms = np.maximum(0.0, np.diff(smooth_rms, prepend=smooth_rms[:1])) * 4.0
    local_on = _moving_mean(onset_norm, max(3, round(0.6 * env_rate)))
    novelty = np.maximum(0.0, onset_norm - 1.1 * local_on) * 0.7 + d_rms * 0.3
    accents_ms = _find_accents(novelty, env_rate)
    tempo_bpm, beat_period_ms, beats_ms = _estimate_tempo_and_beats(novelty, env_rate)

    res = AudioAnalysis(
        env_rate, duration_ms, smooth_rms, onset_norm, contrast, pauses_ms, accents_ms,
        macro_contrast=macro_contrast, tempo_bpm=tempo_bpm, beat_period_ms=beat_period_ms, beats_ms=beats_ms,
    )
    with _ANALYSIS_LOCK:
        existing = _cache_lookup_locked(key, samples)
        if existing is not None:
            return existing
        _analysis_computations += 1
        _cache_store_locked(key, samples, res)
    return res


def _section_contrast(smooth_rms: np.ndarray, env_rate: float, window_s: float) -> np.ndarray:
    n = len(smooth_rms)
    w = max(5 if window_s < 2.0 else 9, round(window_s * env_rate))
    mean_sec = _moving_mean(smooth_rms, w)
    half = w // 2
    past = np.concatenate([np.full(half, mean_sec[0], np.float32), mean_sec[:-half]]) if half < n else mean_sec
    fut = np.concatenate([mean_sec[half:], np.full(half, mean_sec[-1], np.float32)]) if half < n else mean_sec
    return (fut - past).astype(np.float32)


def _find_pauses(smooth_rms: np.ndarray, env_rate: float) -> list[tuple[float, float]]:
    n = len(smooth_rms)
    active_vals = smooth_rms[smooth_rms > 0.05]
    med_rms = float(np.median(active_vals)) if len(active_vals) else 0.0
    pauses_ms: list[tuple[float, float]] = []
    if med_rms <= 0.05:
        return pauses_ms
    quiet = smooth_rms < (0.38 * med_rms)
    min_bins = max(4, round(0.8 * env_rate))
    i = 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i + 1
        while j < n and quiet[j]:
            j += 1
        if j - i >= min_bins:
            pauses_ms.append((i / env_rate * 1000.0, j / env_rate * 1000.0))
        i = j
    return pauses_ms


def _find_accents(novelty: np.ndarray, env_rate: float) -> list[tuple[float, float]]:
    n = len(novelty)
    accents_ms: list[tuple[float, float]] = []
    min_sep = max(3, round(0.50 * env_rate))
    nov_thr = max(0.18, float(np.percentile(novelty, 86))) if n else 1.0
    last_idx = -min_sep
    for idx in range(1, n - 1):
        v = float(novelty[idx])
        if v >= nov_thr and v >= novelty[idx - 1] and v >= novelty[idx + 1]:
            if idx - last_idx >= min_sep:
                accents_ms.append((idx / env_rate * 1000.0, v))
                last_idx = idx
            elif accents_ms and v > accents_ms[-1][1]:
                accents_ms[-1] = (idx / env_rate * 1000.0, v)
                last_idx = idx
    return accents_ms
