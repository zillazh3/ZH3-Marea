"""Alineación del audio capturado con la línea de tiempo de la canción (matemática pura, sin hardware).

Modelo: la posición de la canción avanza 1:1 con el reloj local, `posición(T) = T + c`. Cada consulta a Spotify
da una muestra de `c`; se descarta el retraso de red usando el punto medio del viaje de ida y vuelta.
"""
from dataclasses import dataclass

import numpy as np

from marea.audio.loopback import Recording


@dataclass
class PositionSample:
    progress_ms: float  # lo que reportó Spotify
    t_sent: float  # perf_counter al enviar la consulta
    t_recv: float  # perf_counter al recibir la respuesta

    @property
    def rtt(self) -> float:
        return self.t_recv - self.t_sent

    @property
    def c(self) -> float:
        """Constante del modelo implícita en esta muestra (s): posición - reloj local, en el punto medio del viaje."""
        return self.progress_ms / 1000 - (self.t_sent + self.t_recv) / 2


def estimate_c(samples: list[PositionSample], max_rtt: float = 0.5) -> tuple[float, float]:
    """(c en s, incertidumbre en s). Mediana de las muestras con menor RTT: son las menos afectadas por la red."""
    good = [s for s in samples if s.rtt <= max_rtt]
    if not good:
        raise ValueError("Ninguna medición de posición fue lo bastante rápida para alinear el audio.")
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


def cut(rec: Recording, start_ms: float, end_ms: float, c: float, offset_ms: float = 0.0, fade_ms: float = 150.0) -> np.ndarray:
    """Audio exacto de [start_ms, end_ms] de la canción con fundido de entrada y salida. Rellena con silencio si falta."""
    a = song_time_to_index(rec, start_ms / 1000, c, offset_ms)
    b = a + round((end_ms - start_ms) / 1000 * rec.rate)
    out = np.zeros((b - a, rec.samples.shape[1]), np.float32)
    lo, hi = max(a, 0), min(b, len(rec.samples))
    if hi > lo:
        out[lo - a:hi - a] = rec.samples[lo:hi]
    return apply_fades(out, rec.rate, fade_ms)


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
Z_RELIABLE, RATIO_RELIABLE = 6.0, 1.15  # calibrados con audio sintético: sin relación z≤5 (clips de 12 s: z≤4.2); con relación z≥5.7

@dataclass
class Detection:
    offset_ms: float  # > 0: el audio va retrasado respecto a la letra (se lee más adelante en el audio)
    z: float  # cuánto sobresale el pico del «ruido de fondo» de la correlación a desfases lejanos (en desviaciones)
    ratio: float  # pico / el mejor pico alternativo a más de 150 ms
    at_edge: bool  # el pico quedó en el borde del rango: el desfase real puede estar fuera

    @property
    def reliable(self) -> bool:
        return self.z >= Z_RELIABLE and self.ratio >= RATIO_RELIABLE and not self.at_edge


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


def detect_offset(samples: np.ndarray, rate: int, lyrics, start_ms: float = 0.0, max_lag_ms: float = 1500.0) -> Detection:
    """Desfase entre el audio y la letra: el desplazamiento que mejor alinea los ataques del audio con los inicios de sílaba.
    `samples[0]` es el instante `start_ms` de la canción. Levanta ValueError si no hay con qué estimarlo."""
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    mono = np.asarray(mono, np.float32)
    env, env_rate = onset_envelope(mono, rate)
    if not len(env) or float(env.max()) <= 0:
        raise ValueError("El audio está en silencio: no hay con qué estimar el desfase.")
    # los inicios de ataque se miden contra el nivel local: una sección fuerte no se come a las suaves
    w = max(3, round(0.5 * env_rate))
    csum = np.concatenate([[0.0], np.cumsum(env, dtype=np.float64)])
    local_mean = (csum[w:] - csum[:-w]) / w
    local_mean = np.concatenate([np.full(w // 2, local_mean[0]), local_mean, np.full(len(env) - len(local_mean) - w // 2, local_mean[-1])])
    env = np.maximum(env - 1.2 * local_mean, 0)
    env = env / max(float(np.percentile(env, 95)), 1e-9)

    n = len(env)
    lag = round(max_lag_ms / 1000 * env_rate)  # rango donde se busca el desfase
    null_lag = max(lag, min(round(NULL_S * env_rate), n // 2))  # más allá no puede haber alineación real: sirve de «ruido de fondo»
    pulses = _lyric_impulses(lyrics, start_ms, len(mono) / rate, null_lag / env_rate)
    if len(pulses) < MIN_ONSETS:
        raise ValueError("Hay muy pocas sílabas o versos dentro del audio para estimar el desfase. Prueba con un tramo más largo.")
    # tren de impulsos de la letra y envolvente del audio sobre el mismo eje de tiempo (índice = instante + null_lag),
    # con un poco de holgura (±20 ms) en la letra porque ningún sync es exacto al milisegundo
    length = n + 2 * null_lag
    target = np.zeros(length, np.float32)
    for t, wgt in pulses:
        k = round(t * env_rate) + null_lag
        if 0 <= k < length:
            target[k] += wgt
    sigma = 0.02 * env_rate
    kern = np.exp(-0.5 * (np.arange(-3 * round(sigma) - 1, 3 * round(sigma) + 2) / max(sigma, 1e-6)) ** 2)
    target = np.convolve(target, kern / kern.sum(), mode="same")
    audio = np.zeros(length, np.float32)
    audio[null_lag:null_lag + n] = env

    # puntaje(d) = Σ audio[t + d] · letra[t] para todo d en ±null_lag de una vez (correlación cruzada por FFT, sin envolver)
    size = 1 << (length + null_lag - 1).bit_length()
    cross = np.fft.irfft(np.fft.rfft(audio, size) * np.conj(np.fft.rfft(target, size)), size)
    shifts = np.arange(-null_lag, null_lag + 1)
    scores = np.concatenate([cross[size - null_lag:], cross[:null_lag + 1]])

    inside = np.abs(shifts) <= lag
    cand, cand_shifts = scores[inside], shifts[inside]
    j = int(cand.argmax())
    refined = float(cand_shifts[j])
    if 0 < j < len(cand) - 1:  # interpolación parabólica: precisión por debajo de los 10 ms de la envolvente
        a, b, c = cand[j - 1], cand[j], cand[j + 1]
        denom = a - 2 * b + c
        if denom < 0:
            refined += 0.5 * (a - c) / denom
    # ¿qué tanto sobresale el pico de lo que se obtiene por casualidad con ESTE audio y ESTA letra? el fondo son los desfases
    # lejanos (> rango + 0.3 s): se adapta solo a la longitud del tramo y a cuántas sílabas hay
    far_null = scores[np.abs(shifts) > lag + round(0.3 * env_rate)]
    base_std = float(far_null.std()) if len(far_null) >= 50 else 0.0
    z = (float(cand[j]) - float(far_null.mean())) / base_std if base_std > 0 else 0.0
    other = cand[np.abs(cand_shifts - cand_shifts[j]) > 0.15 * env_rate]
    second = float(other.max()) if len(other) else 0.0
    ratio = float(cand[j]) / second if second > 0 else float("inf")
    # la muestra i de la envolvente describe la ventana centrada en i·hop + ENV_FRAME/2: sin esa media ventana saldría ~10 ms de menos
    return Detection(refined / env_rate * 1000 + 500.0 * ENV_FRAME / rate, z, ratio, j in (0, len(cand) - 1))


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
