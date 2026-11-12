"""Análisis acústico local para wordfx: llena `AudioStats` (tempo/energía vía `analyze_audio`; brillo, tonalidad y tensión vocal aquí).

Solo numpy. Todo el trabajo espectral va sobre una versión diezmada a <= 16 kHz y en FFT por lotes, así que una canción de 4 min cuesta ~1 s.
"""
import hashlib
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from marea.wordfx.fxtypes import AudioStats

CURVE_RATE = 50.0  # 20 ms por muestra de la curva de tensión
_MAX_RATE = 16000

# Calibración de la tensión: (lo, hi) por rasgo; el rasgo vale 0 en `lo` y 1 en `hi`.
# Con stem la señal ya es casi solo voz: los umbrales son bajos. En la mezcla los platillos, las guitarras y los golpes de batería
# suben los tres rasgos sin que nadie fuerce la voz, así que todo el rango se desplaza arriba (más agudos, más plana, más pico para valer 1).
_CAL = {
    True: {"hf": (0.04, 0.30), "flat": (-24.0, -9.0), "peak": (3.0, 9.0)},
    False: {"hf": (0.08, 0.35), "flat": (-16.0, -7.0), "peak": (4.0, 11.0)},
}
_W_HF, _W_ROUGH, _W_PEAK = 0.4, 0.3, 0.3
_SMOOTH_S = 0.3
_MEDIAN_S = 2.0  # ventana de la mediana móvil para detectar picos de volumen

# Krumhansl-Schmuckler (perfiles de tonalidad percibida, tónica primero)
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _to_mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x.mean(axis=1) if x.ndim > 1 else x


def _decimate(mono: np.ndarray, rate: int) -> tuple[np.ndarray, int]:
    """Diezma por media de bloques a <= 16 kHz. # shortcut: antialias pobre (sinc de caja), basta para razones de energía; subir a polifásico si molesta."""
    f = -(-rate // _MAX_RATE)
    if f <= 1:
        return mono, rate
    n = len(mono) // f * f
    return mono[:n].reshape(-1, f).mean(axis=1), rate // f


def _smooth(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1 or len(x) == 0:
        return x
    p = np.pad(x, (w // 2, w - 1 - w // 2), mode="edge")
    c = np.concatenate([[0.0], np.cumsum(p, dtype=np.float64)])
    return ((c[w:] - c[:-w]) / w).astype(np.float32)


def _ramp(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((v - lo) / (hi - lo), 0.0, 1.0)


def strain_curve(signal: np.ndarray, rate: int, *, from_stem: bool = False) -> tuple[np.ndarray, float]:
    """(curva 0..1, 50 Hz). Por trama de 20 ms (ventana Hann de 40 ms): razón de energía 2-8 kHz / total, aspereza (planitud espectral en 1-6 kHz)
    y picos de volumen (RMS sobre su mediana móvil de ~2 s, en dB). Mezcla 0.4/0.3/0.3, suavizada ~0.3 s.
    Calibración en `_CAL`: con stem los umbrales son bajos; con la mezcla completa están desplazados para que música fuerte sin voz forzada no puntúe.
    Las tramas casi mudas (< -45 dB respecto al p95) valen 0: su planitud y su razón de agudos son ruido numérico."""
    x = _to_mono(signal)
    n = int(len(x) / rate * CURVE_RATE)
    if n < 1:
        return np.zeros(0, np.float32), CURVE_RATE
    win = max(16, int(round(2 * rate / CURVE_RATE)))
    centers = np.rint((np.arange(n) + 0.5) * rate / CURVE_RATE).astype(np.int64)
    pad = np.concatenate([np.zeros(win, np.float32), x, np.zeros(win, np.float32)])
    hann = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / rate)
    m_tot, m_hf, m_mid = freqs >= 50, (freqs >= 2000) & (freqs <= 8000), (freqs >= 1000) & (freqs <= 6000)
    rms, hf, fl_db, mid_ratio = (np.empty(n, np.float32) for _ in range(4))
    for a in range(0, n, 2000):
        idx = centers[a:a + 2000, None] - win // 2 + win + np.arange(win)[None, :]
        fr = pad[idx] * hann
        rms[a:a + 2000] = np.sqrt((fr * fr).mean(axis=1) / (hann * hann).mean())
        p = np.abs(np.fft.rfft(fr, axis=1)) ** 2
        tot = p[:, m_tot].sum(axis=1) + 1e-20
        hf[a:a + 2000] = p[:, m_hf].sum(axis=1) / tot
        mid = p[:, m_mid]
        mid_ratio[a:a + 2000] = mid.sum(axis=1) / tot
        mean = mid.mean(axis=1) + 1e-30
        geo = np.exp(np.log(mid + 1e-8 * tot[:, None] / mid.shape[1]).mean(axis=1))  # suelo relativo: sin él un bin a 0 anula la media geométrica
        fl_db[a:a + 2000] = 10 * np.log10(geo / mean + 1e-12)

    ref = max(float(np.percentile(rms, 95)), 1e-9)
    rel_db = 20 * np.log10(np.maximum(rms, 1e-12) / ref)
    gate = _ramp(rel_db, -45.0, -30.0)
    med_w = max(3, int(_MEDIAN_S * CURVE_RATE) | 1)
    med = np.median(sliding_window_view(np.pad(rms, med_w // 2, mode="edge"), med_w), axis=1)
    peak_db = 20 * np.log10(np.maximum(rms, 1e-12) / np.maximum(med, 0.03 * ref))

    cal = _CAL[from_stem]
    rough_gate = _ramp(mid_ratio, 0.0, 0.03)  # sin energía en 1-6 kHz la planitud no significa nada (pads graves)
    score = (_W_HF * _ramp(hf, *cal["hf"]) + _W_ROUGH * _ramp(fl_db, *cal["flat"]) * rough_gate + _W_PEAK * _ramp(peak_db, *cal["peak"])) * gate
    return np.clip(_smooth(score.astype(np.float32), int(round(_SMOOTH_S * CURVE_RATE))), 0.0, 1.0), CURVE_RATE


def _global_strain(curve: np.ndarray) -> float:
    """Media del 25% más alto de la curva: una canción con estribillos rasgados puntúa alto aunque las estrofas sean suaves."""
    if len(curve) == 0:
        return 0.0
    k = max(1, len(curve) // 4)
    return float(np.sort(curve)[-k:].mean())


def spectral_brightness(mono: np.ndarray, rate: int) -> float:
    """0..1: centroide espectral de largo plazo (espectro de potencia sumado en ~48 ventanas repartidas), escala log 300 Hz -> 0, 4 kHz -> 1."""
    x = _to_mono(mono)
    n = 4096
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    starts = np.linspace(0, len(x) - n, 48).astype(np.int64)
    fr = x[starts[:, None] + np.arange(n)[None, :]] * np.hanning(n).astype(np.float32)
    p = (np.abs(np.fft.rfft(fr, axis=1)) ** 2).sum(axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    p[freqs < 20] = 0.0
    if p.sum() < 1e-12:
        return 0.5
    centroid = float((freqs * p).sum() / p.sum())
    return float(np.clip(np.log(max(centroid, 1.0) / 300.0) / np.log(4000.0 / 300.0), 0.0, 1.0))


def key_and_mode(mono: np.ndarray, rate: int) -> tuple[int, float]:
    """(tonalidad 0..11 con C=0 o -1, modo -1 menor..+1 mayor). Croma de picos FFT (110-2000 Hz, interpolación parabólica) sobre <= 60 s centrales,
    correlación con los perfiles Krumhansl-Schmuckler; modo = (mejor corr mayor - mejor corr menor) / 0.25 recortado. Tonalidad = tónica de la mejor."""
    x = _to_mono(mono)
    mid, span = len(x) // 2, 60 * rate
    x, rate = _decimate(x[max(0, mid - span // 2):mid + span // 2], rate)  # se recorta antes de diezmar: es más barato
    n, hop = 8192, 4096
    if len(x) < n:
        return -1, 0.0
    starts = np.arange(0, len(x) - n + 1, hop)
    chroma = np.zeros(12)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    lo, hi = int(np.searchsorted(freqs, 110.0)), int(np.searchsorted(freqs, 2000.0))
    win = np.hanning(n).astype(np.float32)
    for a in range(0, len(starts), 64):
        fr = x[starts[a:a + 64, None] + np.arange(n)[None, :]] * win
        mag = np.abs(np.fft.rfft(fr, axis=1))
        m = mag[:, lo - 1:hi + 1]
        peak = (m[:, 1:-1] > m[:, :-2]) & (m[:, 1:-1] >= m[:, 2:]) & (m[:, 1:-1] > 0.05 * m.max(axis=1, keepdims=True) + 1e-12)
        r, c = np.nonzero(peak)
        k = c + 1  # índice en m
        y0, y1, y2 = m[r, k - 1], m[r, k], m[r, k + 1]
        d = 0.5 * (y0 - y2) / np.maximum(y0 - 2 * y1 + y2, -1e-12) if len(r) else np.zeros(0)
        f = (k + lo - 1 + np.clip(d, -0.5, 0.5)) * (rate / n)
        pc = np.rint(12 * np.log2(f / 440.0) + 69).astype(np.int64) % 12
        np.add.at(chroma, pc, np.sqrt(y1))  # sqrt: comprime el dominio de las notas fuertes y sus armónicos
    if chroma.sum() < 1e-9 or np.ptp(chroma) < 1e-9:
        return -1, 0.0
    best = {}
    for name, prof in (("maj", _KS_MAJOR), ("min", _KS_MINOR)):
        corr = np.array([np.corrcoef(chroma, np.roll(prof, t))[0, 1] for t in range(12)])
        best[name] = (int(np.argmax(corr)), float(corr.max()))
    (kmaj, cmaj), (kmin, cmin) = best["maj"], best["min"]
    if max(cmaj, cmin) < 0.3:
        return -1, 0.0
    return (kmaj if cmaj >= cmin else kmin), float(np.clip((cmaj - cmin) / 0.25, -1.0, 1.0))


def analyze(samples: np.ndarray, rate: int, *, stem: np.ndarray | None = None, stem_rate: int = 16000, analysis=None) -> AudioStats:
    """Llena todos los campos de `AudioStats`. `stem` (voz separada, mono) da la tensión vocal; sin él se usa la mezcla (más conservadora).
    `strain` global = media del 25% más alto de la curva."""
    if analysis is None:
        from marea.model.cinematic import analyze_audio
        analysis = analyze_audio(samples, rate)
    mono = _to_mono(samples)
    rms = np.asarray(analysis.rms, dtype=np.float32)
    energy = float(np.clip(rms, 0.0, 1.0).mean()) if len(rms) else 0.5
    key, mode = key_and_mode(mono, rate)
    from_stem = stem is not None and len(stem) > 0
    if from_stem:
        curve, crate = strain_curve(stem, stem_rate, from_stem=True)
    else:
        curve, crate = strain_curve(*_decimate(mono, rate), from_stem=False)
    return AudioStats(
        duration_ms=len(mono) / max(1, rate) * 1000.0,
        tempo_bpm=float(analysis.tempo_bpm or 0.0),
        energy=energy,
        brightness=spectral_brightness(mono, rate),
        mode=mode,
        key=key,
        strain=_global_strain(curve),
        strain_curve=curve,
        strain_rate=crate,
        from_stem=from_stem,
    )


def find_cached_stem(audio_path, lab_root=None) -> np.ndarray | None:
    """Stem mono float32 de la caché del sidecar (`<lab>/cache/<sha256 del archivo>/lead-*.npy`, si no `separate-*.npy`; el más reciente) o None.
    No lanza nunca por archivos ausentes o corruptos."""
    try:
        if lab_root is None:
            from marea.lab import env
            from marea.paths import make_settings
            custom = make_settings().value("experimental/lab_dir", "")  # misma clave que marea.ui.lab.lab_root
            lab_root = Path(custom) if custom else env.lab_dir()
        h = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        d = Path(lab_root) / "cache" / h.hexdigest()
        for pattern in ("lead-*.npy", "separate-*.npy"):
            for p in sorted(d.glob(pattern), key=lambda q: q.stat().st_mtime, reverse=True):
                try:
                    a = np.load(p, allow_pickle=False)
                except (OSError, ValueError):
                    continue
                if a.ndim == 1 and a.size and np.issubdtype(a.dtype, np.floating):
                    return a.astype(np.float32, copy=False)
    except Exception:  # contrato: jamás propagar (la UI sigue sin stem)
        return None
    return None
