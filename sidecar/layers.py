"""Detección acústica de capas vocales y partición de flujos independientes (solo NumPy, sin Torch).

Principios:
- Separar voz/instrumentos (BS-Roformer) NO separa voz principal de coros: sin stems `lead`/`backing`, las capas simultáneas que no
  puedan distinguirse con certeza acústica quedan `role="uncertain"` para revisión humana.
- Nunca se clasifica una capa solo por texto o pausas: se mide relación de energía entre stems, correlación de envolvente en banda
  vocal (200–4000 Hz) y seguimiento de F0 / polifonía armónica.
- Nunca se concatenan ni recortan líneas que se solapan: `partition_streams` separa las tomas simultáneas en pasadas independientes.
"""
from __future__ import annotations

import statistics
import numpy as np

from sidecar.pipeline import FRAME_MS, frame_energy

BLEED_DB = -12.0  # por debajo de -12 dB con envolvente síncrona respecto al lead es fuga del separador, no coro real
BLEED_CORR = 0.78
MIN_VOICE_RMS = 1.5e-3
BG_ROLES = ("backing", "harmony", "adlib")


def _slice_pcm(pcm: np.ndarray, start_ms: int, end_ms: int, rate: int = 16000) -> np.ndarray:
    a = max(0, min(len(pcm), int(round(start_ms * rate / 1000))))
    b = max(a, min(len(pcm), int(round(end_ms * rate / 1000))))
    return pcm[a:b]


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    x = a[:n].astype(np.float64) - float(np.mean(a[:n]))
    y = b[:n].astype(np.float64) - float(np.mean(b[:n]))
    den = float(np.sqrt(np.dot(x, x) * np.dot(y, y)))
    return float(np.clip(np.dot(x, y) / den, -1.0, 1.0)) if den > 1e-12 else 0.0


def stem_metrics(lead_pcm: np.ndarray, back_pcm: np.ndarray, start_ms: int, end_ms: int, rate: int = 16000) -> dict:
    """Relación acústica entre el stem principal y el secundario en `[start_ms, end_ms]`."""
    seg_l = _slice_pcm(lead_pcm, start_ms, end_ms, rate)
    seg_b = _slice_pcm(back_pcm, start_ms, end_ms, rate)
    el = frame_energy(seg_l, rate=rate)
    eb = frame_energy(seg_b, rate=rate)
    rms_l = float(np.sqrt(np.mean(seg_l.astype(np.float64) ** 2))) if len(seg_l) else 0.0
    rms_b = float(np.sqrt(np.mean(seg_b.astype(np.float64) ** 2))) if len(seg_b) else 0.0
    el_mean = float(np.mean(el)) if len(el) else rms_l
    eb_mean = float(np.mean(eb)) if len(eb) else rms_b
    ratio_db = float(20.0 * np.log10((eb_mean + 1e-8) / (el_mean + 1e-8)))
    corr = _pearson(el, eb)
    peak_b = float(np.percentile(eb, 90)) if len(eb) else 0.0
    floor = max(MIN_VOICE_RMS, 0.22 * peak_b)
    active = eb >= floor if len(eb) else np.zeros(0, bool)
    active_frac = float(np.mean(active)) if len(active) else 0.0
    active_ms = round(float(np.sum(active)) * FRAME_MS)
    is_bleed = rms_b < MIN_VOICE_RMS or (ratio_db < BLEED_DB and corr > BLEED_CORR)
    return {
        "rms_lead": rms_l,
        "rms_back": rms_b,
        "ratio_db": round(ratio_db, 2),
        "env_corr": round(corr, 3),
        "active_frac": round(active_frac, 3),
        "active_ms": active_ms,
        "is_bleed": bool(is_bleed),
    }


def pitch_track(pcm: np.ndarray, rate: int = 16000, fmin: float = 85.0, fmax: float = 880.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Seguimiento por frame (20 ms) de `(f0_hz, voicing, polyphony)` mediante autocorrelación normalizada en banda vocal."""
    win, hop, nfft = 640, 320, 2048  # 40 ms cada 20 ms
    if len(pcm) < win:
        z = np.zeros(0, np.float32)
        return z, z, z
    n = (len(pcm) - win) // hop + 1
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = pcm[idx].astype(np.float32) * np.hanning(win).astype(np.float32)[None, :]
    spec = np.fft.rfft(frames, nfft, axis=1)
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    spec[:, (freqs < 75.0) | (freqs > 3600.0)] = 0.0
    power = np.abs(spec) ** 2
    acf = np.fft.irfft(power, nfft, axis=1)[:, :win]
    norm = np.maximum(acf[:, :1], 1e-10)
    acf = acf / norm

    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    lag_min = max(2, int(rate / fmax))
    lag_max = min(win - 2, int(rate / fmin))
    f0 = np.zeros(n, np.float32)
    voicing = np.zeros(n, np.float32)
    poly = np.zeros(n, np.float32)
    if lag_max <= lag_min:
        return f0, voicing, poly

    sub = acf[:, lag_min:lag_max + 1]
    lags = np.arange(lag_min, lag_max + 1, dtype=np.float32)
    for i in range(n):
        if rms[i] < MIN_VOICE_RMS:
            continue
        row = sub[i]
        # Primer pico fuerte (evita elegir el doble del periodo / subarmónico de octava).
        pmax = float(np.max(row))
        if pmax <= 0.2:
            continue
        peaks = [k for k in range(1, len(row) - 1) if row[k] >= row[k - 1] and row[k] >= row[k + 1] and row[k] >= 0.82 * pmax]
        best = peaks[0] if peaks else int(np.argmax(row))
        tau0 = float(lags[best])
        f0[i] = rate / tau0
        voicing[i] = float(np.clip(row[best], 0.0, 1.0))

        # Saliencia polifónica: suprime múltiplos y submúltiplos armónicos de tau0 y mide el mayor periodo independiente.
        resid = row.copy()
        for mult in (0.25, 1.0 / 3.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
            center = tau0 * mult
            tol = max(2.0, 0.07 * center)
            resid[np.abs(lags - center) <= tol] = 0.0
        sec = float(np.max(resid)) if len(resid) else 0.0
        poly[i] = float(np.clip(sec / max(voicing[i], 0.25), 0.0, 1.0)) if voicing[i] >= 0.35 else 0.0
    return f0, voicing, poly


def _overlap_fraction(start_ms: int, end_ms: int, intervals: list[tuple[int, int]]) -> float:
    dur = max(1, end_ms - start_ms)
    ov = sum(max(0, min(end_ms, b) - max(start_ms, a)) for a, b in intervals if b > start_ms and a < end_ms)
    return min(1.0, ov / dur)


def classify_layer(*, start_ms: int, end_ms: int, lead_intervals: list[tuple[int, int]],
                   lead_pcm: np.ndarray | None = None, back_pcm: np.ndarray | None = None,
                   mix_pcm: np.ndarray | None = None, on_backing_stem: bool = False,
                   rate: int = 16000) -> tuple[str, float]:
    """Clasifica acústicamente una capa vocal en `(role, role_confidence)`.

    Sin stems `lead`/`backing` separados nunca finge distinguir coros de armonías o ad-libs: devuelve `("uncertain", < 0.6)`.
    """
    ov_frac = _overlap_fraction(start_ms, end_ms, lead_intervals)
    if lead_pcm is not None and back_pcm is not None:
        m = stem_metrics(lead_pcm, back_pcm, start_ms, end_ms, rate=rate)
        seg_l = _slice_pcm(lead_pcm, start_ms, end_ms, rate)
        seg_b = _slice_pcm(back_pcm, start_ms, end_ms, rate)
        f0_l, v_l, _ = pitch_track(seg_l, rate=rate)
        f0_b, v_b, _ = pitch_track(seg_b, rate=rate)
        n = min(len(f0_l), len(f0_b))

        if not on_backing_stem and m["ratio_db"] < -3.5:
            conf = float(np.clip(0.65 + min(0.32, (-m["ratio_db"] - 3.5) / 25.0), 0.55, 0.98))
            return "lead", round(conf, 3)

        if m["is_bleed"]:
            return "uncertain", 0.2

        # 1) Armonía paralela: superpuesta con el lead, envolvente acoplada e intervalo tonal estable (terceras/cuartas/quintas/sextas).
        if n >= 6 and ov_frac >= 0.35 and m["env_corr"] >= 0.45:
            voiced = (v_l[:n] >= 0.42) & (v_b[:n] >= 0.42) & (f0_l[:n] > 0) & (f0_b[:n] > 0)
            if float(np.mean(voiced)) >= 0.25:
                st = np.abs(12.0 * np.log2(f0_b[:n][voiced] / f0_l[:n][voiced]))
                folded = np.minimum(st % 12.0, 12.0 - (st % 12.0))
                med_st = float(np.median(folded))
                iqr_st = float(np.percentile(folded, 75) - np.percentile(folded, 25))
                if 2.0 <= med_st <= 9.5 and iqr_st <= 2.6:
                    conf = float(np.clip(0.66 + 0.22 * m["env_corr"] + 0.1 * (1.0 - iqr_st / 3.0), 0.6, 0.95))
                    return "harmony", round(conf, 3)

        # 2) Dueto / voz co-principal simultánea: nivel comparable al lead y fraseo rítmico independiente.
        if ov_frac >= 0.25 and -3.5 <= m["ratio_db"] <= 4.0 and m["env_corr"] < 0.32 and m["active_ms"] >= 850:
            conf = float(np.clip(0.64 + 0.2 * (0.32 - m["env_corr"]), 0.6, 0.9))
            return "duet", round(conf, 3)

        # 3) Ad-lib: intervención vocal breve o esporádica desacoplada de la envolvente del lead.
        if (m["active_ms"] <= 1050 or m["active_frac"] < 0.52) and m["env_corr"] < 0.35 and m["ratio_db"] > -7.5:
            if m["ratio_db"] > -4.5:
                conf = float(np.clip(0.62 + 0.2 * min(1.0, (m["ratio_db"] + 4.5) / 10.0), 0.6, 0.9))
                return "adlib", round(conf, 3)
            return "uncertain", 0.46

        # 4) Voz de fondo / coro sostenido o respuesta claramente presente en el stem secundario.
        if m["ratio_db"] > -6.5 and m["active_frac"] >= 0.35:
            conf = float(np.clip(0.65 + 0.25 * min(1.0, (m["ratio_db"] + 6.5) / 12.0), 0.6, 0.94))
            return "backing", round(conf, 3)

        return "uncertain", round(float(np.clip(0.35 + 0.03 * (m["ratio_db"] + 12.0), 0.25, 0.52)), 3)

    if mix_pcm is not None:
        seg = _slice_pcm(mix_pcm, start_ms, end_ms, rate)
        _, v, poly = pitch_track(seg, rate=rate)
        voiced_poly = float(np.mean(poly[v >= 0.4])) if np.any(v >= 0.4) else 0.0
        return "uncertain", round(float(np.clip(0.30 + 0.25 * voiced_poly, 0.25, 0.55)), 3)

    return "uncertain", 0.35


def detect_polyphonic_overlaps(pcm: np.ndarray, primary_lines: list[dict], rate: int = 16000,
                               min_dur_ms: int = 380) -> list[dict]:
    """Detecta tramos con evidencia acústica de polifonía vocal dentro de un único stem (sin inventar texto ni rol específico)."""
    _, voicing, poly = pitch_track(pcm, rate=rate)
    if len(poly) == 0:
        return []
    active = (voicing >= 0.45) & (poly >= 0.58)
    # Suavizado morfológico breve (cierra huecos de 1-2 frames).
    if len(active) >= 3:
        pad = np.pad(active.astype(np.int32), 1)
        active = (pad[:-2] + pad[1:-1] + pad[2:]) >= 2
    primary = [(int(l["start"]), int(l["end"])) for l in primary_lines if "start" in l and "end" in l]
    regions = []
    i, n = 0, len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < n and active[j]:
            j += 1
        s_ms, e_ms = round(i * FRAME_MS), round(j * FRAME_MS)
        if e_ms - s_ms >= min_dur_ms and _overlap_fraction(s_ms, e_ms, primary) >= 0.25:
            conf = round(float(np.clip(np.mean(poly[i:j]) * 0.7, 0.25, 0.55)), 3)
            regions.append({"start": s_ms, "end": e_ms, "role": "uncertain", "role_confidence": conf})
        i = j
    return regions


def has_active_backing(lead_pcm: np.ndarray, back_pcm: np.ndarray, rate: int = 16000) -> bool:
    """¿Contiene el stem secundario energía vocal real por encima del umbral de fuga? Evita correr Whisper sobre silencio."""
    eb = frame_energy(back_pcm, rate=rate)
    if len(eb) == 0 or float(np.max(eb)) < MIN_VOICE_RMS * 10:
        return False
    el = frame_energy(lead_pcm, rate=rate)
    n = min(len(el), len(eb))
    if n == 0:
        return False
    strong = eb[:n] >= max(MIN_VOICE_RMS * 8, 0.28 * float(np.percentile(eb[:n], 95)))
    if int(np.sum(strong)) * FRAME_MS < 240:
        return False
    independent = eb[:n] > 0.30 * (el[:n] + 1e-6)
    return int(np.sum(strong & independent)) * FRAME_MS >= 200


def partition_streams(lines: list[dict]) -> tuple[list[int], list[list[int]]]:
    """Separa los índices de `lines` en:
    - `backbone`: flujo principal secuencial no solapado (un solo Viterbi de canción completa preserva estribillos repetidos).
    - `secondary`: lista de flujos independientes (coros/armonías/ad-libs o duetos/capas que se solapan con `backbone`).
    """
    backbone: list[int] = []
    rest: list[int] = []
    last_end = -1
    last_start = -1
    for idx, line in enumerate(lines):
        role = line.get("role") or ("backing" if line.get("bg") else ("duet" if line.get("agent", "v1") != "v1" else "lead"))
        is_bg = bool(line.get("bg")) or role in BG_ROLES
        layer = int(line.get("layer", 0))
        hint = line.get("hint")
        if hint or is_bg or layer > 0 or line.get("source_stem") == "backing":
            rest.append(idx)
            continue
        if hint and len(hint) == 2:
            hs, he = int(hint[0]), int(hint[1])
            # Pequeño solape de cola (≤ 150 ms) entre versos sucesivos de la misma voz es legítimo; un solape real es otra capa.
            if hs < last_start or hs < last_end - 150:
                rest.append(idx)
                continue
            last_start, last_end = hs, max(last_end, he)
        backbone.append(idx)

    # Agrupar `rest`: líneas sin `hint` ni `parent` que comparten `(agent, role, layer)` y no se solapan van en su propio flujo;
    # líneas con `hint` o `parent` se alinean individualmente en su ventana acotada para que ninguna recorte ni empuje a otra.
    secondary: list[list[int]] = []
    unhinted_groups: dict[tuple, list[int]] = {}
    for idx in rest:
        line = lines[idx]
        if line.get("hint") or line.get("parent"):
            secondary.append([idx])
        else:
            key = (line.get("agent", "v1"), line.get("role", ""), int(line.get("layer", 0)), bool(line.get("bg")), line.get("source_stem", ""))
            unhinted_groups.setdefault(key, []).append(idx)
    secondary.extend(unhinted_groups.values())
    return backbone, secondary


def estimate_clock_shift(lines: list[dict], aligned_by_idx: dict[int, dict]) -> int:
    """Mediana de `(aligned.start - hint[0])` sobre el flujo principal para centrar ventanas de capas secundarias en el reloj del audio."""
    deltas = []
    for idx, res in aligned_by_idx.items():
        hint = lines[idx].get("hint")
        if hint and len(hint) == 2:
            deltas.append(int(res["start"]) - int(hint[0]))
    return round(statistics.median(deltas)) if len(deltas) >= 3 else 0


def layer_window(line: dict, aligned_by_id: dict[str, dict], clock_shift_ms: int, duration_ms: int,
                 margin_ms: int = 2500) -> tuple[int, int] | None:
    """Ventana `[a_ms, b_ms]` en el reloj del audio para alinear una capa independiente sin arrastrarla a otro estribillo."""
    hint = line.get("hint")
    if hint and len(hint) == 2 and hint[1] > hint[0]:
        a = max(0, int(hint[0]) + clock_shift_ms - margin_ms)
        b = min(duration_ms, int(hint[1]) + clock_shift_ms + margin_ms)
        if b - a >= 200:
            return a, b
    return None
