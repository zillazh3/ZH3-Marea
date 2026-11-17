"""Alineación del audio capturado con la línea de tiempo de la canción (matemática pura, sin hardware).

Modelo: la posición de la canción avanza 1:1 con el reloj local, `posición(T) = T + c`. Cada consulta a Spotify
da una muestra de `c`; se descarta el retraso de red usando el punto medio del viaje de ida y vuelta.
"""
from dataclasses import dataclass

import numpy as np

from marea.audio.loopback import Recording
from marea.i18n import t
from marea.providers.errors import PositionSample


def estimate_c(samples: list[PositionSample], max_rtt: float = 0.5) -> tuple[float, float]:
    """(c en s, incertidumbre en s). Mediana de las muestras con menor RTT: son las menos afectadas por la red."""
    good = [s for s in samples if s.rtt <= max_rtt]
    if not good:
        raise ValueError(t("errors.audio.noMeasurements"))
    good.sort(key=lambda s: s.rtt)
    best = good[: max(1, (len(good) + 1) // 2)]
    cs = np.array([s.c for s in best])
    return float(np.median(cs)), float(max(np.ptp(cs) / 2, np.median([s.rtt for s in best]) / 2))


def song_time_to_index(rec: Recording, song_s: float, c: float, offset_ms: float = 0.0) -> int:
    """Índice de muestra de `rec` que suena en el instante `song_s` de la canción.

    offset_ms > 0 = el audio llega más tarde de lo que dice el modelo (latencia de salida): se adelanta el corte.
    """
    local_t = song_s - c + offset_ms / 1000
    return round((local_t - rec.t0) * rec.rate)


def slice_padded(samples: np.ndarray, rate: int, start_sample: int, n_samples: int, fade_ms: float = 150.0) -> np.ndarray:
    """Extract `n_samples` starting at `start_sample`, zero-padding out-of-range regions and applying symmetric fades."""
    out = np.zeros((n_samples, samples.shape[1]), np.float32)
    lo, hi = max(start_sample, 0), min(start_sample + n_samples, len(samples))
    if hi > lo:
        out[lo - start_sample:hi - start_sample] = samples[lo:hi]
    return apply_fades(out, rate, fade_ms)


def cut(rec: Recording, start_ms: float, end_ms: float, c: float, offset_ms: float = 0.0, fade_ms: float = 150.0) -> np.ndarray:
    """Audio exacto de [start_ms, end_ms] de la canción con fundido de entrada y salida. Rellena con silencio si falta."""
    a = song_time_to_index(rec, start_ms / 1000, c, offset_ms)
    n = round((end_ms - start_ms) / 1000 * rec.rate)
    return slice_padded(rec.samples, rec.rate, a, n, fade_ms)


def apply_fades(samples: np.ndarray, rate: int, fade_ms: float) -> np.ndarray:
    n = min(round(fade_ms / 1000 * rate), len(samples) // 2)
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
        samples[:n] *= ramp
        samples[-n:] *= ramp[::-1]
    return samples


# ---------------- desfase automático: audio vs. tiempos de la letra ----------------

ENV_FRAME = 1024  # muestras por ventana del espectro (≈21 ms a 48 kHz): ataques bien localizados
BAND_HZ = (200, 4000)  # donde vive la voz; el bombo y los platillos quedan casi fuera
MIN_ONSETS = 8  # menos sílabas/versos que eso dentro del audio no dan una correlación fiable
NULL_S = 15.0  # hasta qué desfase (s) se mide el «ruido de fondo» de la correlación
Z_RELIABLE, RATIO_RELIABLE = 4.0, 1.15
MARGIN_RELIABLE, DRIFT_MAX_MS = 1.20, 250.0
W_VAD = 0.65  # peso de la envolvente de actividad vocal sobre la curva de ataques (en unidades Z)


@dataclass
class Detection:
    offset_ms: float  # > 0: el audio va retrasado respecto a la letra (se lee más adelante en el audio)
    z: float  # cuánto sobresale el pico del «ruido de fondo» de la correlación a desfases lejanos (MAD-Z)
    ratio: float  # sobresaliente / mejor pico alternativo (>200 ms), medido sobre la mediana
    at_edge: bool  # el pico quedó en el borde del rango: el desfase real puede estar fuera
    margin: float | None = None  # diferencia Z entre el mejor pico y el mejor alternativo a >200 ms
    drift_ms: float | None = None  # discrepancia entre primera y segunda mitad (detecta drift o empate rítmico)

    @property
    def reliable(self) -> bool:
        if self.at_edge or self.z < Z_RELIABLE:
            return False
        if self.margin is not None:
            if self.margin < MARGIN_RELIABLE:
                return False
        elif self.ratio < RATIO_RELIABLE:
            return False
        if self.drift_ms is not None and self.drift_ms > DRIFT_MAX_MS:
            return False
        return True


def onset_envelope(mono: np.ndarray, rate: int) -> tuple[np.ndarray, float]:
    """Fuerza de ataque (flujo espectral en log, banda de la voz) -> (envolvente, muestras por segundo). La muestra i
    corresponde al instante (i·hop + ENV_FRAME/2)/rate del audio."""
    hop = max(1, round(rate / 100))
    n = 1 + (len(mono) - ENV_FRAME) // hop
    if n < 2:
        return np.zeros(0, np.float32), rate / hop
    win = np.hanning(ENV_FRAME).astype(np.float32)
    k0, k1 = int(BAND_HZ[0] * ENV_FRAME / rate), int(BAND_HZ[1] * ENV_FRAME / rate) + 1
    cols = np.arange(ENV_FRAME)[None, :]
    flux, prev = np.zeros(n, np.float32), None
    for s in range(0, n, 2000):  # por bloques: no se materializa toda la canción en ventanas
        idx = np.arange(s, min(s + 2000, n))
        mag = np.abs(np.fft.rfft(mono[idx[:, None] * hop + cols] * win, axis=1))[:, k0:k1]
        logm = np.log1p(30.0 * mag).astype(np.float32)  # compresión: importan los ataques, no el volumen
        diff = np.diff(logm, axis=0, prepend=logm[:1] if prev is None else prev[None, :])
        flux[idx] = np.maximum(diff, 0).sum(axis=1)
        prev = logm[-1]
    flux[0] = 0.0
    return flux, rate / hop


def _lyric_impulses(lyrics, start_ms: float, duration_s: float, margin_s: float) -> list[tuple[float, float]]:
    """[(segundo dentro del audio, peso)]: inicio de palabra 1, sílaba dentro de palabra 0.5, primer sílaba de verso 2. Los versos
    con tiempos repartidos (sin sílabas reales) solo aportan su inicio: el resto es inventado."""
    out = []
    for line in lyrics.lines:
        for i, s in enumerate(line.syllables):
            if line.approx and i:
                continue
            prev_ends_word = i == 0 or line.syllables[i - 1].word_end
            w = 2.0 if i == 0 else 1.0 if prev_ends_word else 0.5
            t = (s.begin - start_ms) / 1000
            if -margin_s <= t <= duration_s + margin_s:
                out.append((t, w))
    return out


def _moving_avg(x: np.ndarray, half_win: int, axis: int) -> np.ndarray:
    """Apply an edge-padded box filter along `axis` using a prefix sum."""
    if half_win <= 0 or x.shape[axis] <= 1:
        return x
    pad_width = [(0, 0)] * x.ndim
    pad_width[axis] = (half_win, half_win)
    xp = np.pad(x, pad_width, mode="edge")
    cs = np.cumsum(xp, axis=axis, dtype=np.float64)
    zero_shape = list(xp.shape)
    zero_shape[axis] = 1
    cs = np.concatenate([np.zeros(zero_shape, dtype=np.float64), cs], axis=axis)
    w = 2 * half_win + 1
    sl_hi = [slice(None)] * x.ndim
    sl_lo = [slice(None)] * x.ndim
    sl_hi[axis] = slice(w, w + x.shape[axis])
    sl_lo[axis] = slice(0, x.shape[axis])
    return ((cs[tuple(sl_hi)] - cs[tuple(sl_lo)]) / w).astype(np.float32)


def _prepare_mid_side(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Return `(mono_mid, stereo_side)` while avoiding anti-phase cancellation on inverted L/R channels."""
    if arr.ndim == 2 and arr.shape[1] >= 2:
        left, right = arr[:, 0], arr[:, 1]
        mid = (left + right)
        mid *= 0.5
        side = (left - right)
        side *= 0.5
        n = max(1, len(mid))
        rms_m = float(np.sqrt(np.dot(mid, mid) / n))
        rms_l = float(np.sqrt(np.dot(left, left) / n))
        rms_r = float(np.sqrt(np.dot(right, right) / n))
        if rms_m < 0.1 * max(rms_l, rms_r, 1e-12):
            return (np.ascontiguousarray(left if rms_l >= rms_r else right), None)
        rms_s = float(np.sqrt(np.dot(side, side) / n))
        return mid, (side if rms_s >= 1e-4 * max(rms_m, 1e-12) else None)
    return (arr.reshape(-1) if arr.ndim > 1 else arr), None


def _extract_vocal_cues(samples: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Extract harmonic-masked vocal onset flux and vocal activity envelope plus `(fps, frame_shift_s)`."""
    arr = np.asarray(samples, np.float32)
    dec = max(1, round(rate / 16000)) if rate >= 24000 else 1
    if dec > 1:
        n_trim = (len(arr) // dec) * dec
        if n_trim == 0:
            return np.zeros(0, np.float32), np.zeros(0, np.float32), rate / 160.0, 0.0
        if arr.ndim == 2:
            arr = arr[:n_trim].reshape(-1, dec, arr.shape[1]).mean(axis=1)
        else:
            arr = arr[:n_trim].reshape(-1, dec).mean(axis=1)
        work_rate = rate / dec
    else:
        work_rate = float(rate)
    mono, side = _prepare_mid_side(arr)

    n_fft = 512 if work_rate >= 12000 else 256
    hop = max(1, round(work_rate / 100.0))
    n = 1 + (len(mono) - n_fft) // hop
    if n < 4:
        return np.zeros(0, np.float32), np.zeros(0, np.float32), work_rate / hop, 0.0

    win = np.hanning(n_fft).astype(np.float32)
    cols = np.arange(n_fft)[None, :]
    mag_mid = np.empty((n, n_fft // 2 + 1), dtype=np.float32)
    mag_side = np.empty_like(mag_mid) if side is not None else None
    for s in range(0, n, 2000):
        idx = np.arange(s, min(s + 2000, n))
        mag_mid[idx] = np.abs(np.fft.rfft(mono[idx[:, None] * hop + cols] * win, axis=1)).astype(np.float32)
        if mag_side is not None:
            mag_side[idx] = np.abs(np.fft.rfft(side[idx[:, None] * hop + cols] * win, axis=1)).astype(np.float32)

    if mag_side is not None:
        m2 = mag_mid * mag_mid
        s2 = mag_side * mag_side
        cw = (m2 / (m2 + 1.2 * s2 + 1e-12)).astype(np.float32)
        mag_voc = mag_mid * (0.25 + 0.75 * cw)
    else:
        mag_voc = mag_mid

    freqs = np.fft.rfftfreq(n_fft, 1.0 / work_rate)
    n_bins = mag_voc.shape[1]
    k_lo = min(n_bins - 2, int(np.searchsorted(freqs, 220.0)))
    k_hi = max(k_lo + 2, min(n_bins, int(np.searchsorted(freqs, 3800.0))))
    band = mag_voc[:, k_lo:k_hi]
    fps = work_rate / hop
    half_t = max(2, round(0.15 * fps))
    df = freqs[1] - freqs[0]
    half_f = max(2, round(250.0 / max(df, 1e-6)))
    harm = _moving_avg(band, half_t, axis=0)
    perc = _moving_avg(band, half_f, axis=1)
    h2 = harm * harm
    p2 = perc * perc
    h_mask = h2 / (h2 + p2 + 1e-12)

    # Blend 75% harmonic mask with 25% raw band so both harmonic vocal entries and fast plosives register.
    spec_on = band * (0.25 + 0.75 * h_mask)
    logm = np.log1p(30.0 * spec_on)
    diff = np.diff(logm, axis=0, prepend=logm[:1])
    e_on = np.maximum(diff, 0.0).sum(axis=1).astype(np.float32)
    e_on[0] = 0.0
    e_on = np.maximum(0.0, e_on - 0.65 * _moving_avg(e_on, max(3, round(0.35 * fps)), axis=0))

    kv0 = min(n_bins - 2, int(np.searchsorted(freqs, 300.0)))
    kv1 = max(kv0 + 2, min(n_bins, int(np.searchsorted(freqs, 3400.0))))
    v_rms = np.sqrt(np.mean(mag_voc[:, kv0:kv1] ** 2, axis=1))
    tot = np.sqrt(np.mean(mag_mid[:, 1:] ** 2, axis=1))
    pos = tot > 0
    if np.any(pos):
        scale = float(np.percentile(tot[pos], 95))
        act = tot > 0.03 * scale
        vad_raw = np.log1p(50.0 * v_rms / max(scale, 1e-9))
        med = float(np.percentile(vad_raw[act], 30)) if np.any(act) else 0.0
        e_vad = np.where(act, np.maximum(0.0, vad_raw - med), 0.0).astype(np.float32)
    else:
        e_vad = np.zeros(n, np.float32)

    return e_on, e_vad, fps, (n_fft * 0.5) / work_rate


def _add_impulse(arr: np.ndarray, idx: int, weight: float) -> None:
    m = len(arr)
    for o in (-2, -1, 0, 1, 2):
        j = idx + o
        if 0 <= j < m:
            arr[j] += weight * np.exp(-0.5 * (o / 1.2) ** 2)


def _lyric_targets(lyrics, start_ms: float, dur_s: float, max_lag_s: float,
                   fps: float, frame_shift_s: float, m: int, t0_grid: float) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """Build padded lyric onset-impulse and vocal-activity arrays on the STFT frame grid."""
    lyr_on = np.zeros(m, np.float32)
    lyr_vad = np.zeros(m, np.float32)
    imp_times: list[float] = []
    for line in lyrics.lines:
        if line.approx:
            if line.syllables:
                t = (line.syllables[0].begin - start_ms) / 1000.0
                if -max_lag_s <= t <= dur_s + max_lag_s:
                    imp_times.append(t)
                    _add_impulse(lyr_on, int(round((t - frame_shift_s - t0_grid) * fps)), 2.0)
        else:
            for i, s in enumerate(line.syllables):
                prev_end = i == 0 or line.syllables[i - 1].word_end
                w = 2.0 if i == 0 else (1.0 if prev_end else 0.5)
                t = (s.begin - start_ms) / 1000.0
                if -max_lag_s <= t <= dur_s + max_lag_s:
                    imp_times.append(t)
                    _add_impulse(lyr_on, int(round((t - frame_shift_s - t0_grid) * fps)), w)
        if not line.approx and line.syllables:
            for s in line.syllables:
                b = (s.begin - start_ms) / 1000.0 - frame_shift_s
                e = max((s.end - start_ms) / 1000.0 - frame_shift_s, b + 0.05)
                i0, i1 = max(0, int(round((b - t0_grid) * fps))), min(m, int(round((e - t0_grid) * fps)))
                if i1 > i0:
                    lyr_vad[i0:i1] = 1.0
        else:
            b = (line.start - start_ms) / 1000.0 - frame_shift_s
            e = max((line.end - start_ms) / 1000.0 - frame_shift_s, b + 0.05)
            i0, i1 = max(0, int(round((b - t0_grid) * fps))), min(m, int(round((e - t0_grid) * fps)))
            if i1 > i0:
                lyr_vad[i0:i1] = 1.0
    return lyr_on, lyr_vad, imp_times


def _mad_z(arr: np.ndarray) -> np.ndarray:
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med))) * 1.4826
    return (arr - med) / max(mad, 1e-9)


def _xcorr(aud: np.ndarray, ref: np.ndarray) -> np.ndarray:
    a = aud - float(np.mean(aud))
    n_fft = 1 << int(np.ceil(np.log2(max(2, len(a) + len(ref)))))
    return np.fft.irfft(np.fft.rfft(a, n_fft) * np.conj(np.fft.rfft(ref, n_fft)), n_fft)


def detect_offset(samples: np.ndarray, rate: int, lyrics, start_ms: float = 0.0,
                  max_lag_ms: float = 1500.0, *, vocal_stem: np.ndarray | None = None,
                  vocal_rate: int = 16000) -> Detection:
    """Estimate signed audio delay (ms) that aligns audio vocal cues to lyrics timestamps starting at `start_ms`."""
    use_stem = (
        vocal_stem is not None
        and len(vocal_stem) > 0
        and float(np.max(np.abs(vocal_stem))) > 0.0
    )
    e_on, e_vad, fps, frame_shift_s = (
        _extract_vocal_cues(vocal_stem, vocal_rate) if use_stem else _extract_vocal_cues(samples, rate)
    )
    if len(e_on) < round(0.5 * fps) or float(e_on.max()) <= 0.0:
        raise ValueError(t("errors.audio.silent"))

    n = len(e_on)
    dur_s = n / fps
    max_lag_s = max(0.0, float(max_lag_ms) / 1000.0)
    pad_s = max(max_lag_s, NULL_S)
    pad_f = int(round(pad_s * fps))
    m = n + 2 * pad_f
    t0_grid = -pad_s

    lyr_on, lyr_vad, imp_times = _lyric_targets(lyrics, start_ms, dur_s, pad_s, fps, frame_shift_s, m, t0_grid)
    if len(imp_times) < MIN_ONSETS:
        raise ValueError(t("errors.audio.tooFewSyllables"))

    d_search = int(round(max_lag_s * fps))
    d_null = min(pad_f - 10, max(d_search + round(2.0 * fps), round(0.75 * n)))
    d_null = max(d_null, d_search)
    lags = np.arange(-d_null, d_null + 1)

    xc_on = _xcorr(e_on, lyr_on)
    idxs = (lags - pad_f) % len(xc_on)
    comb = _mad_z(xc_on[idxs])

    has_vad = float(np.any(e_vad > 0)) and float(np.any(lyr_vad[pad_f:pad_f + n] > 0))
    if has_vad:
        lyr_vad_zm = lyr_vad - float(np.mean(lyr_vad[pad_f:pad_f + n]))
        xc_vad = _xcorr(e_vad, lyr_vad_zm)
        comb = comb + W_VAD * _mad_z(xc_vad[idxs])

    in_win = (lags >= -d_search) & (lags <= d_search)
    wl, wc = lags[in_win], comb[in_win]
    bi = int(np.argmax(wc))
    bl, bv = int(wl[bi]), float(wc[bi])
    at_edge = bi in (0, len(wc) - 1)
    refined = float(bl)
    if 0 < bi < len(wc) - 1:
        y0, y1, y2 = wc[bi - 1], wc[bi], wc[bi + 1]
        denom = y0 - 2.0 * y1 + y2
        if denom < 0.0:
            refined += float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))

    far = np.abs(lags - bl) > max(1, round(0.5 * fps))
    fv = comb[far] if np.any(far) else comb
    f_med = float(np.median(fv))
    f_mad = float(np.median(np.abs(fv - f_med))) * 1.4826
    z_mad = (bv - f_med) / max(f_mad, 1e-9) if f_mad > 0 else 0.0

    other = wc[np.abs(wl - bl) > max(1, round(0.2 * fps))]
    second = float(other.max()) if len(other) else f_med
    margin = bv - second
    ratio = (bv - f_med) / max(second - f_med, 1e-6) if second > f_med else (float("inf") if bv > f_med else 1.0)

    drift_ms = 0.0
    if len(imp_times) >= 16 and (max(imp_times) - min(imp_times)) >= 12.0:
        f_mid = int(round((float(np.median(imp_times)) - frame_shift_s - t0_grid) * fps))
        m1_on, m2_on = lyr_on.copy(), lyr_on.copy()
        m1_on[f_mid:] = 0.0
        m2_on[:f_mid] = 0.0
        z1 = _mad_z(_xcorr(e_on, m1_on)[idxs])
        z2 = _mad_z(_xcorr(e_on, m2_on)[idxs])
        if has_vad:
            m1_v, m2_v = lyr_vad.copy(), lyr_vad.copy()
            m1_v[f_mid:] = 0.0
            m2_v[:f_mid] = 0.0
            m1_v -= float(np.mean(m1_v[pad_f:pad_f + n]))
            m2_v -= float(np.mean(m2_v[pad_f:pad_f + n]))
            z1 = z1 + W_VAD * _mad_z(_xcorr(e_vad, m1_v)[idxs])
            z2 = z2 + W_VAD * _mad_z(_xcorr(e_vad, m2_v)[idxs])
        l1 = int(wl[int(np.argmax(z1[in_win]))])
        l2 = int(wl[int(np.argmax(z2[in_win]))])
        drift_ms = abs(l1 - l2) / fps * 1000.0

    return Detection(refined / fps * 1000.0, z_mad, ratio, at_edge, margin=margin, drift_ms=drift_ms)


def find_onsets(mono: np.ndarray, rate: int, threshold: float = 0.3, min_gap_s: float = 0.15) -> list[float]:
    """Tiempos (s) de los golpes/clics de una señal: primer cruce del umbral (relativo al pico) tras un silencio."""
    env = np.abs(mono)
    peak = float(env.max()) if len(env) else 0.0
    if peak <= 0:
        return []
    above = np.flatnonzero(env >= threshold * peak)
    onsets, last = [], -1e9
    for i in above:
        if i / rate - last >= min_gap_s:
            onsets.append(i / rate)
        last = i / rate
    return onsets
