"""Anzuelo: sugiere los mejores rangos de versos para un clip de TikTok (15 / 30 / 60 s o a medida).

Módulo puro (sin Qt ni Skia). Entrada: la letra (tiempos de verso), opcionalmente el análisis de audio local y los conteos de
efectos por palabra. Salida: sugerencias ordenadas y diversificadas, cada una con su desglose de puntaje y una explicación en
español. Determinista: la misma entrada da el mismo resultado.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

BUNDLED_WEIGHTS = Path(__file__).resolve().parent.parent.parent / "assets" / "hooks" / "weights.json"
# clave -> (mínimo, máximo, valor por omisión). Todo lo que viene del JSON se limita a estos rangos; lo desconocido se ignora.
WEIGHT_SPEC: dict[str, tuple[float, float, float]] = {
    "repetition": (0, 10, 3.0), "title": (0, 10, 1.5), "energy": (0, 10, 2.0), "onset": (0, 10, 1.0),
    "section_entry": (0, 10, 1.0), "wordfx": (0, 10, 0.5), "chorus_section": (0, 10, 3.0),  # reservado: aún no hay secciones
    "duration_fit": (0, 10, 1.0), "penalty_intro_outro": (0, 10, 1.5), "penalty_mid_phrase": (0, 10, 1.0),
    "tol_low": (0.3, 1.0, 0.75), "tol_high": (1.0, 1.5, 1.05), "preroll_ms": (0, 1500, 400),
    "similarity": (0.5, 1.0, 0.85), "max_overlap": (0.1, 1.0, 0.5), "top_n": (1, 20, 5),
}
MIN_NORM_CHARS = 6  # una línea más corta ("ooh", "yeah") no cuenta como repetición
SAT_REPEATS = 3.0  # con tantas repeticiones la señal de repetición llega a 1
PHRASE_GAP_MS = 300


def load_weights(user: Path | None = None, bundled: Path = BUNDLED_WEIGHTS) -> dict[str, float]:
    """Pesos del JSON editable (usuario sobre integrado), limitados a su rango."""
    out = {k: v[2] for k, v in WEIGHT_SPEC.items()}
    paths = [bundled] + ([user] if user else [])
    if user is None:
        try:
            from marea.paths import app_data
            paths.append(app_data() / "hooks" / "weights.json")
        except Exception:  # sin carpeta de datos: valen los integrados
            pass
    for p in paths:
        try:
            raw = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k, v in (raw.items() if isinstance(raw, dict) else ()):
            if k in WEIGHT_SPEC and isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v):
                lo, hi, _ = WEIGHT_SPEC[k]
                out[k] = float(min(hi, max(lo, v)))
    return out


@dataclass(frozen=True)
class Suggestion:
    first: int  # índices de verso (de 0), ambos incluidos
    last: int
    start_ms: float  # inicio con pre-roll
    end_ms: float  # fin del último verso + outro
    target_s: float
    score: float  # 0..100
    breakdown: dict = field(compare=False, hash=False, default_factory=dict)
    explanation: str = ""
    confidence: str = "media"  # alta | media | baja

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


def normalize(text: str) -> str:
    """Minúsculas, sin acentos, sin (ad-libs) ni puntuación."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text.lower())
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def _similar(a: str, b: str, thr: float) -> bool:
    if len(a) < MIN_NORM_CHARS or len(b) < MIN_NORM_CHARS:
        return False
    if a == b:
        return True
    sm = SequenceMatcher(None, a, b, autojunk=False)
    return sm.real_quick_ratio() >= thr and sm.quick_ratio() >= thr and sm.ratio() >= thr


def repetition_maps(norms: list[str], thr: float) -> tuple[list[set[int]], list[set[int]]]:
    """(por verso: versos distintos con los que forma un *bloque* repetido de ≥2 versos seguidos,
    por verso: versos con la misma línea suelta). Detecta el coro como bloque, no solo como línea."""
    n = len(norms)
    sim = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            sim[i][j] = sim[j][i] = _similar(norms[i], norms[j], thr)
    run = [[0] * (n + 1) for _ in range(n + 1)]  # run[i][j]: versos seguidos parecidos desde (i, j)
    for i in range(n - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            run[i][j] = run[i + 1][j + 1] + 1 if i != j and sim[i][j] else 0
    block: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            r = run[i][j]
            if r >= 2 and not (i > 0 and j > 0 and run[i - 1][j - 1]):  # solo el inicio de cada tramo
                for k in range(r):
                    block[i + k].add(j + k)
                    block[j + k].add(i + k)
    single = [{j for j in range(n) if j != i and sim[i][j]} for i in range(n)]
    return block, single


def _ts(ms: float) -> str:
    s = int(max(0, ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def suggest(lyrics, title: str = "", targets_s=(15, 30, 60), *, analysis=None, audio_offset_ms: float = 0.0,
            fx_counts=None, weights: dict | None = None, outro_ms: float = 500.0) -> dict[float, list[Suggestion]]:
    """Mejores rangos por duración objetivo. `analysis`: marea.audio.analysis.AudioAnalysis o None (solo letra, confianza menor).
    `fx_counts`: palabras con efecto por verso (opcional). `outro_ms`: la cola que el export añade tras el último verso."""
    w = {**{k: v[2] for k, v in WEIGHT_SPEC.items()}, **(weights if weights is not None else load_weights())}
    lines = lyrics.lines
    n = len(lines)
    if n == 0:
        return {float(t): [] for t in targets_s}
    norms = [normalize(l.text) for l in lines]
    block, single = repetition_maps(norms, w["similarity"])
    rep_line = [min(1.0, max(len(block[k]), 0.5 * len(single[k])) / SAT_REPEATS) for k in range(n)]
    tnorm = normalize(title)
    has_title = [1.0 if len(tnorm) >= 3 and tnorm in norms[k] else 0.0 for k in range(n)]
    fx = [float(c) for c in fx_counts] if fx_counts is not None and len(fx_counts) == n else [0.0] * n
    starts = np.array([l.start for l in lines], float)
    ends = np.array([l.end for l in lines], float)

    def pref(arr):
        return np.concatenate([[0.0], np.cumsum(arr)])

    p_rep, p_title, p_fx = pref(rep_line), pref(has_title), pref(fx)
    audio = analysis is not None and len(analysis.rms) > 0
    if audio:
        rate = float(analysis.rate)
        rms, onset, macro = (np.asarray(a, np.float32) for a in (analysis.rms, analysis.onset, analysis.macro_contrast))
        rms_ref = max(float(np.percentile(rms, 90)), 1e-6)
        on_ref = max(float(np.percentile(onset, 90)), 1e-6) if len(onset) else 1.0
        mac_ref = max(float(np.percentile(macro, 95)), 1e-6) if len(macro) else 1.0
        p_rms, p_on = pref(rms), pref(onset)

        def idx(ms: float, size: int) -> int:
            return int(min(max(0, round((ms + audio_offset_ms) / 1000.0 * rate)), size))

    def audio_signals(a_ms: float, b_ms: float) -> tuple[float, float, float, float]:
        i0, i1 = idx(a_ms, len(rms)), idx(b_ms, len(rms))
        if i1 <= i0:
            return 0.0, 0.0, 0.0, a_ms
        energy = min(1.0, float((p_rms[i1] - p_rms[i0]) / (i1 - i0)) / rms_ref)
        peak_i = i0 + int(np.argmax(rms[i0:i1]))
        j0, j1 = idx(a_ms, len(onset)), idx(b_ms, len(onset))
        dens = min(1.0, float((p_on[j1] - p_on[j0]) / max(1, j1 - j0)) / on_ref) if len(onset) else 0.0
        m0, m1 = idx(a_ms - 1000, len(macro)), idx(a_ms + 2000, len(macro))
        entry = min(1.0, max(0.0, float(macro[m0:m1].max())) / mac_ref) if m1 > m0 else 0.0
        return energy, dens, entry, peak_i / rate * 1000.0 - audio_offset_ms

    results: dict[float, list[Suggestion]] = {}
    pre = w["preroll_ms"]
    for target in targets_s:
        T = float(target) * 1000.0
        cands = _candidates(starts, ends, pre, outro_ms, T, w["tol_low"], w["tol_high"])
        if not cands:  # canción o letra más corta que la ventana: se amplía una vez para no devolver nada
            cands = _candidates(starts, ends, pre, outro_ms, T, 0.0, 1.6 * w["tol_high"])
        scored: list[Suggestion] = []
        for a, b, dur in cands:
            cnt = b - a + 1
            rep = float(p_rep[b + 1] - p_rep[a]) / cnt
            ttl = min(1.0, float(p_title[b + 1] - p_title[a]) / 2.0)
            fxd = min(1.0, float(p_fx[b + 1] - p_fx[a]) / cnt / 2.0)
            fit = max(0.0, 1.0 - abs(dur - 0.97 * T) / (0.3 * T))
            s_a, s_b = starts[a] - pre, ends[b] + outro_ms
            energy = dens = entry = 0.0
            peak_ms = starts[a]
            if audio:
                energy, dens, entry, peak_ms = audio_signals(starts[a], ends[b])
            intro = 1.0 if a == 0 else 0.5 if a == 1 else 0.0
            outro = 1.0 if b >= n - 1 else 0.5 if b == n - 2 else 0.0
            mid = 0.0
            if a > 0 and starts[a] - ends[a - 1] < PHRASE_GAP_MS and not re.search(r"[.!?…]\s*$", lines[a - 1].text):
                mid += 1.0
            if b < n - 1 and starts[b + 1] - ends[b] < PHRASE_GAP_MS and not re.search(r"[.!?…]\s*$", lines[b].text):
                mid += 0.5
            parts = {"repetition": rep, "title": ttl, "wordfx": fxd, "duration_fit": fit}
            if audio:
                parts.update(energy=energy, onset=dens, section_entry=entry)
            pos = sum(w[k] * v for k, v in parts.items())
            neg = w["penalty_intro_outro"] * (intro + outro) / 2.0 + w["penalty_mid_phrase"] * min(1.0, mid)
            denom = sum(w[k] for k in parts) or 1.0
            score = max(0.0, 100.0 * (pos - neg) / denom)
            conf = 0.35 + 0.35 * audio + 0.30 * min(1.0, rep * 2)
            scored.append(Suggestion(
                a, b, max(0.0, s_a), s_b, float(target), round(score, 2),
                {**{k: round(v, 3) for k, v in parts.items()}, "penalty_intro_outro": round((intro + outro) / 2, 3),
                 "penalty_mid_phrase": round(min(1.0, mid), 3), "confidence": round(conf, 2)},
                _explain(block, single, a, b, ttl, rep, energy, entry, peak_ms, audio),
                "alta" if conf >= 0.8 else "media" if conf >= 0.55 else "baja"))
        scored.sort(key=lambda s: (-s.score, s.first, s.last))
        chosen: list[Suggestion] = []
        for s in scored:
            if all(_overlap(s, c) <= w["max_overlap"] for c in chosen):
                chosen.append(s)
                if len(chosen) >= int(w["top_n"]):
                    break
        results[float(target)] = chosen
    return results


def _candidates(starts, ends, pre: float, outro: float, T: float, lo: float, hi: float) -> list[tuple[int, int, float]]:
    """(primer verso, último verso, duración del clip) con inicio y fin en límites de verso y duración dentro de [lo·T, hi·T]."""
    out, n = [], len(starts)
    for a in range(n):
        t0 = max(0.0, starts[a] - pre)
        for b in range(a, n):
            dur = ends[b] + outro - t0
            if dur > hi * T:
                break
            if dur >= lo * T:
                out.append((a, b, dur))
    return out


def _overlap(a: Suggestion, b: Suggestion) -> float:
    inter = min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms)
    return max(0.0, inter) / max(1.0, min(a.duration_ms, b.duration_ms))


def _explain(block, single, a, b, ttl, rep, energy, entry, peak_ms, audio) -> str:
    bits = []
    hits = [len(block[k]) for k in range(a, b + 1) if block[k]]
    if hits:
        bits.append(f"coro repetido {1 + int(np.median(hits))}×")
    else:
        sh = [len(single[k]) for k in range(a, b + 1) if single[k]]
        if sh:
            bits.append(f"verso que se repite {1 + max(sh)}×")
    if ttl > 0:
        bits.append("menciona el título")
    if audio and energy >= 0.8:
        bits.append(f"pico vocal en {_ts(peak_ms)}")
    if audio and entry >= 0.6:
        bits.append("entra con subida de energía")
    return ", ".join(bits) if bits else "tramo completo con buena duración"
