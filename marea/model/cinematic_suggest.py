"""Musical suggestion engine for cinematic intervals (`compute_opening_trim_ms`, `suggest_intervals`)."""
from __future__ import annotations

import copy
import hashlib
import math
from typing import Callable

import numpy as np

from marea.audio.analysis import AudioAnalysis, analyze_audio
from marea.engine.curves import clamp, clamp01
from marea.model.cinematic import (
    DEFAULT_INTERVAL_MS,
    MAX_COVERAGE,
    MIN_GAP_MS,
    MIN_INTERVAL_MS,
    CinematicInterval,
    CinematicTreatment,
    VideoResource,
)
from marea.model.lyrics import Lyrics


def _snap_to_accent(t_ms: float, accents_ms: list[tuple[float, float]], radius_ms: float = 450.0) -> float:
    best_t, best_v = t_ms, -1.0
    for acc_t, strength in accents_ms:
        if abs(acc_t - t_ms) <= radius_ms and strength > best_v:
            best_t, best_v = acc_t, strength
    return best_t


def _snap_to_beat(t_ms: float, beats_ms: list[float], radius_ms: float = 280.0) -> float:
    if not beats_ms:
        return t_ms
    nearest = min(beats_ms, key=lambda b: abs(b - t_ms))
    return nearest if abs(nearest - t_ms) <= radius_ms else t_ms


def _q10(ms: float) -> float:
    """Quantize to 10 ms (0.01 s) increments to match editor controls."""
    return round(float(ms) / 10.0) * 10.0


def compute_opening_trim_ms(
    video_duration_ms: float,
    song_duration_ms: float,
    resource: VideoResource | None = None,
) -> float:
    """Compute initial video trim aligned to song duration, skipping dead intro frames and landing after clean cuts."""
    v_total = max(0.0, float(resource.duration_ms if (resource and resource.duration_ms > 0) else video_duration_ms))
    if v_total <= 0.0:
        return 0.0

    if resource is not None and resource.duration_ms > 0:
        v_first, v_last = resource.active_bounds_ms
        cuts = [float(c) for c in resource.scene_cuts_ms if v_first <= c < v_last]
        peaks = [float(p) for p in resource.salience_peaks_ms if v_first <= p < v_last]
    else:
        v_first, v_last = 0.0, v_total
        cuts, peaks = [], []

    s_dur = max(0.0, float(song_duration_ms))
    active_span = max(0.0, v_last - v_first)
    if s_dur <= 0.0 or active_span <= s_dur:
        return _q10(v_first)

    excess = active_span - s_dur
    target_open = v_last - s_dur
    if not cuts and not peaks:
        return _q10(clamp(target_open, v_first, max(v_first, v_total - MIN_INTERVAL_MS)))

    tol_back = min(3200.0, 0.35 * excess)
    tol_fwd = min(1400.0, max(0.0, (v_total - v_last) * 0.5 + min(650.0, 0.02 * s_dur)))
    lo = max(v_first, target_open - tol_back)
    hi = min(max(v_first, v_last - min(s_dur * 0.85, v_last - v_first)), target_open + tol_fwd)
    if hi < lo:
        hi = lo

    entry_cands: list[tuple[float, bool]] = [(target_open, False)]
    if lo <= v_first <= hi:
        entry_cands.append((v_first, False))
    for c in cuts:
        entry = c + 40.0
        if lo <= entry <= hi:
            entry_cands.append((entry, True))

    best_t, best_score = target_open, -1e9
    for cand, is_shot_start in entry_cands:
        score = -abs(cand - target_open) / 1400.0 + (1.35 if is_shot_start else 0.0)
        if any(40.0 < (c - cand) < 900.0 for c in cuts):
            score -= 1.80
        if any(-500.0 <= (cand - p) <= 700.0 for p in peaks):
            score -= 1.15
        if score > best_score:
            best_score, best_t = score, cand

    return _q10(clamp(best_t, v_first, max(v_first, v_total - MIN_INTERVAL_MS)))


def song_fingerprint(
    lyrics: Lyrics,
    analysis: AudioAnalysis | None = None,
    t_min: float = 0.0,
    t_max: float = 0.0,
) -> int:
    """Deterministic structural seed per song."""
    h = hashlib.blake2b(digest_size=8)
    h.update(f"{round(t_min)}:{round(t_max)}:{len(lyrics.lines)}:{len(lyrics.interludes)}".encode())
    if lyrics.lines:
        idxs = sorted({0, len(lyrics.lines) // 3, (2 * len(lyrics.lines)) // 3, len(lyrics.lines) - 1})
        for i in idxs:
            ln = lyrics.lines[i]
            h.update(f"|{round(ln.start)}-{round(ln.end)}:{len(ln.syllables)}:{len(ln.text)}".encode())
    if analysis is not None and len(analysis.rms):
        q = np.quantile(analysis.rms, [0.2, 0.5, 0.8])
        h.update(
            f"|bpm={round(analysis.tempo_bpm * 10)}:p={len(analysis.pauses_ms)}:a={len(analysis.accents_ms)}"
            f":q={round(float(q[0]) * 100)},{round(float(q[1]) * 100)},{round(float(q[2]) * 100)}".encode()
        )
    return int.from_bytes(h.digest(), "little")


def _estimate_pacing(lyrics: Lyrics, analysis: AudioAnalysis | None, t_min: float, t_max: float) -> tuple[float, float]:
    """Return `(pacing 0..1, beat_period_ms)` combining tempo, accent density, and syllable cadence."""
    span_s = max(1.0, (t_max - t_min) / 1000.0)
    lines = [ln for ln in lyrics.lines if ln.end > t_min and ln.start < t_max]
    n_syl = sum(max(1, len(ln.syllables)) for ln in lines)
    vocal_s = max(1.0, sum(max(0.1, (min(t_max, ln.end) - max(t_min, ln.start)) / 1000.0) for ln in lines))
    syl_rate = n_syl / vocal_s if lines else 2.6
    syl_pacing = clamp01((syl_rate - 1.6) / 3.4)

    if analysis is not None and len(analysis.rms):
        tempo_pacing = clamp01((analysis.tempo_bpm - 68.0) / 92.0)
        acc_in = sum(1 for t, _ in analysis.accents_ms if t_min <= t <= t_max)
        acc_pacing = clamp01((acc_in / span_s - 0.15) / 1.15)
        pacing = clamp01(0.45 * tempo_pacing + 0.35 * acc_pacing + 0.20 * syl_pacing)
        beat_ms = clamp(analysis.beat_period_ms, 325.0, 960.0)
    else:
        line_rate = len(lines) / span_s
        pacing = clamp01(0.60 * syl_pacing + 0.40 * clamp01((line_rate - 0.12) / 0.35))
        beat_ms = clamp(60000.0 / (76.0 + 72.0 * pacing), 360.0, 880.0)
    return pacing, beat_ms


def _range_bounds(lyrics: Lyrics, audio_samples: np.ndarray | None, sample_rate: int,
                  range_ms: tuple[float, float] | None, song_duration_ms: float | None) -> tuple[float, float]:
    if range_ms is not None:
        return max(0.0, float(range_ms[0])), float(range_ms[1])
    if song_duration_ms is not None and song_duration_ms > 0:
        return 0.0, float(song_duration_ms)
    if lyrics.lines:
        return 0.0, float(max(l.end for l in lyrics.lines) + 1500.0)
    return 0.0, len(audio_samples) / max(1, sample_rate) * 1000.0


def _collect_interlude_candidates(lyrics: Lyrics, t_min: float, t_max: float, beat_ms: float, max_cand_dur: float,
                                  rng: np.random.Generator, quantize_beats: Callable, add_cand: Callable) -> None:
    for il in lyrics.interludes:
        if il.end <= t_min or il.start >= t_max:
            continue
        il_s, il_e = max(t_min, float(il.start)), min(t_max, float(il.end))
        dur = il_e - il_s
        if dur < 1700.0:
            continue
        if dur >= 11500.0:
            d1 = quantize_beats(clamp(0.42 * dur * float(rng.uniform(0.88, 1.12)), 3600.0, max_cand_dur), 6, 15)
            add_cand(3.3 + min(1.0, dur / 8000.0), il_s, min(il_e, il_s + d1), "interlude", gap_scale=1.10)
            d2 = quantize_beats(clamp(0.36 * dur * float(rng.uniform(0.88, 1.12)), 3200.0, max_cand_dur - 400.0), 5, 13)
            add_cand(3.0, max(il_s, il_e - d2 + 450.0), il_e + 500.0, "interlude", gap_scale=1.05, rising=True)
        else:
            pad_in = beat_ms * 0.65 if il.start > 0 else 0.0
            pad_out = beat_ms * float(rng.uniform(0.7, 1.3))
            add_cand(3.2 + min(1.2, dur / 5000.0), il_s - pad_in, il_e + pad_out, "interlude", gap_scale=1.08)
    if lyrics.lines:
        last_end = float(max(l.end for l in lyrics.lines))
        if t_max - last_end >= 2000.0:
            outro_dur = quantize_beats(clamp((t_max - last_end) * float(rng.uniform(0.75, 1.0)), 2800.0, 6600.0), 5, 14)
            add_cand(2.7, last_end - 220.0, min(t_max, last_end + outro_dur), "interlude", gap_scale=1.05)


def _collect_audio_candidates(analysis: AudioAnalysis | None, t_min: float, t_max: float, audio_offset_ms: float,
                              pacing: float, beat_ms: float, min_cand_dur: float,
                              accents: list[tuple[float, float]], beats: list[float],
                              rng: np.random.Generator, quantize_beats: Callable, add_cand: Callable) -> None:
    if analysis is None or len(analysis.contrast) <= 10:
        return
    c_abs = np.abs(analysis.contrast)
    if float(c_abs.max()) >= 0.13 and float(np.percentile(c_abs, 85)) >= 0.035:
        min_sep = max(4, round(1.75 * analysis.rate))
        thr = max(0.13, float(np.percentile(c_abs, 82)))
        last_i = -min_sep
        for idx in range(2, len(c_abs) - 2):
            v = float(c_abs[idx])
            if v >= thr and v >= c_abs[idx - 1] and v >= c_abs[idx + 1] and idx - last_i >= min_sep:
                t_song = idx / analysis.rate * 1000.0 - audio_offset_ms
                if t_min <= t_song <= t_max:
                    t_anchor = _snap_to_beat(_snap_to_accent(t_song, accents, 420.0), beats, 240.0)
                    rising = bool(analysis.contrast[idx] > 0)
                    lead_beats = float(rng.uniform(0.75, 1.45)) if rising else float(rng.uniform(0.45, 0.95))
                    base_w = (4300.0 - 1400.0 * pacing) if rising else (3600.0 - 1100.0 * pacing)
                    dur_w = quantize_beats(base_w * float(rng.uniform(0.78, 1.24)), 3, 12)
                    s = t_anchor - lead_beats * beat_ms
                    add_cand(2.55 + min(1.45, v * 2.1), s, s + dur_w, "transition", gap_scale=0.92, rising=rising)
                    last_i = idx

    if len(analysis.macro_contrast) > 15:
        m_abs = np.abs(analysis.macro_contrast)
        if float(m_abs.max()) >= 0.15 and float(np.percentile(m_abs, 85)) >= 0.04:
            m_sep = max(8, round(4.0 * analysis.rate))
            m_thr = max(0.15, float(np.percentile(m_abs, 86)))
            last_m = -m_sep
            for idx in range(3, len(m_abs) - 3):
                mv = float(m_abs[idx])
                if mv >= m_thr and mv >= m_abs[idx - 1] and mv >= m_abs[idx + 1] and idx - last_m >= m_sep:
                    t_song = idx / analysis.rate * 1000.0 - audio_offset_ms
                    if t_min <= t_song <= t_max:
                        t_anchor = _snap_to_beat(_snap_to_accent(t_song, accents, 500.0), beats, 260.0)
                        rising = bool(analysis.macro_contrast[idx] > 0)
                        dur_w = quantize_beats((4900.0 - 1200.0 * pacing) * float(rng.uniform(0.84, 1.20)), 5, 13)
                        s = t_anchor - beat_ms * (1.2 if rising else 0.7)
                        add_cand(2.45 + min(1.3, mv * 1.9), s, s + dur_w, "transition", gap_scale=1.0, rising=rising)
                        last_m = idx

    for p0, p1 in analysis.pauses_ms:
        s_song, e_song = p0 - audio_offset_ms, p1 - audio_offset_ms
        p_dur = e_song - s_song
        if e_song > t_min and s_song < t_max and p_dur >= 1150.0:
            mid = 0.5 * (s_song + e_song)
            dur = quantize_beats(clamp((p_dur + 1350.0) * float(rng.uniform(0.85, 1.18)), 2400.0, 5800.0), 4, 12)
            add_cand(2.35 + min(0.6, p_dur / 4500.0), mid - 0.5 * dur, mid + 0.5 * dur, "pause", gap_scale=1.02)

    if accents:
        acc_strengths = [sv for _, sv in accents]
        acc_p70 = float(np.percentile(acc_strengths, 70)) if len(acc_strengths) >= 3 else 0.25
        acc_gate = max(0.24, acc_p70)
        last_acc_t = -1e9
        min_acc_step = max(1400.0, 2.8 * beat_ms)
        for acc_t, acc_v in accents:
            if t_min <= acc_t <= t_max and acc_v >= acc_gate and (acc_t - last_acc_t) >= min_acc_step:
                anchor = _snap_to_beat(acc_t, beats, 220.0)
                n_beats = int(rng.choice([3, 4, 5, 6] if pacing >= 0.48 else [4, 5, 6, 8]))
                dur_a = clamp(n_beats * beat_ms, min_cand_dur, 4200.0)
                lead = beat_ms * float(rng.uniform(0.25, 0.70))
                add_cand(1.75 + min(1.25, acc_v * 1.6), anchor - lead, anchor - lead + dur_a, "accent", gap_scale=0.82, rising=True)
                last_acc_t = acc_t


def _collect_lyric_candidates(lyrics: Lyrics, analysis: AudioAnalysis | None, t_min: float, t_max: float,
                              pacing: float, beat_ms: float, accents: list[tuple[float, float]], beats: list[float],
                              rng: np.random.Generator, quantize_beats: Callable, add_cand: Callable) -> list:
    lines = [l for l in lyrics.lines if l.end > t_min and l.start < t_max]
    seen_norms: set[str] = set()
    prev_norm = ""
    prev_syl_rate = 0.0
    for i, line in enumerate(lines):
        line_dur = max(120.0, float(line.end - line.start))
        max_syl = max(((s.end - s.begin) for s in line.syllables), default=0)
        duet_switch = i > 0 and line.agent != lines[i - 1].agent
        phrase_gap = (line.start - lines[i - 1].end) if i > 0 else 0
        accent_hit = any(abs(a_t - line.start) <= 290.0 and a_v >= 0.28 for a_t, a_v in accents)

        norm_txt = " ".join(line.text.lower().split())
        chorus_return = bool(len(norm_txt) >= 6 and norm_txt in seen_norms and norm_txt != prev_norm)
        cur_syl_rate = max(1, len(line.syllables)) / (line_dur / 1000.0)
        cadence_shift = (
            analysis is None and i > 0 and prev_syl_rate > 0
            and (cur_syl_rate / prev_syl_rate >= 1.58 or cur_syl_rate / prev_syl_rate <= 0.62)
        )

        score = 0.0
        if max_syl >= 680 and not line.approx:
            score += 1.25 + min(0.85, (max_syl - 680) / 950.0)
        if line.bg:
            score += 0.95
        if duet_switch:
            score += 1.10
        if 550 <= phrase_gap < 2400:
            score += 0.65 + (0.25 if phrase_gap >= 1050 else 0.0)
        if accent_hit:
            score += 0.90
        if chorus_return:
            score += 1.15
        if cadence_shift:
            score += 0.85

        if norm_txt:
            seen_norms.add(norm_txt)
        prev_norm = norm_txt
        prev_syl_rate = cur_syl_rate

        if score >= 1.05:
            end_line = float(line.end)
            use_two_lines = (
                end_line - line.start < (2900.0 - 700.0 * pacing)
                and i + 1 < len(lines)
                and lines[i + 1].start - end_line < 1100.0
                and float(rng.random()) < (0.72 - 0.30 * pacing)
            )
            if use_two_lines:
                end_line = float(lines[i + 1].end)
            s_anchor = _snap_to_beat(_snap_to_accent(float(line.start), accents, 270.0), beats, 220.0)
            s = s_anchor - beat_ms * float(rng.uniform(0.35, 0.85))
            min_phrase_w = quantize_beats((2900.0 - 900.0 * pacing) * float(rng.uniform(0.82, 1.18)), 3, 9)
            e = max(s + min_phrase_w, end_line + beat_ms * float(rng.uniform(0.45, 1.05)))
            add_cand(1.15 + score, s, e, "transition" if chorus_return else "accent", gap_scale=0.88, rising=accent_hit or chorus_return)
    return lines


def _select_candidates(candidates: list[tuple[float, float, float, str, float, bool]], span: float, pacing: float,
                       min_cand_dur: float, rng: np.random.Generator) -> list[tuple[float, float, str, float, bool]]:
    target_cov = clamp(0.32 + 0.08 * pacing + float(rng.uniform(-0.045, 0.045)), 0.25, MAX_COVERAGE - 0.015)
    budget_ms = min(MAX_COVERAGE * span, max(min_cand_dur + 400.0, target_cov * span))
    hard_cap_ms = MAX_COVERAGE * span
    ideal_cycle_ms = clamp(11500.0 - 4800.0 * pacing, 5200.0, 13000.0)

    scored_pool = [
        (base_sc + float(rng.normal(0.0, 0.34)), base_sc, _q10(s), _q10(e), kind, gap, rising)
        for base_sc, s, e, kind, gap, rising in candidates
        if _q10(e) - _q10(s) >= max(MIN_INTERVAL_MS, min_cand_dur - 40.0)
    ]

    chosen: list[tuple[float, float, str, float, bool]] = []
    used_ms = 0.0
    used_indices: set[int] = set()
    while True:
        best_idx, best_priority = -1, -1e9
        for idx_c, (eff_sc, _base_sc, s, e, kind, gap, rising) in enumerate(scored_pool):
            if idx_c in used_indices:
                continue
            dur = e - s
            if (chosen and used_ms + dur > budget_ms) or (not chosen and dur > hard_cap_ms and span >= 5000.0):
                continue
            conflict = False
            nearest_dist = span
            for cs, ce, _ck, cgap, _cr in chosen:
                req_gap = max(MIN_GAP_MS, min(gap, cgap))
                if not (e + req_gap <= cs or s >= ce + req_gap):
                    conflict = True
                    break
                d = (cs - e) if e <= cs else (s - ce)
                if d < nearest_dist:
                    nearest_dist = d
            if conflict:
                continue
            spacing_bonus = 0.90 * (1.0 - math.exp(-nearest_dist / ideal_cycle_ms)) if chosen else 0.0
            priority = eff_sc + spacing_bonus
            if priority > best_priority:
                best_priority, best_idx = priority, idx_c
        if best_idx < 0:
            break
        used_indices.add(best_idx)
        _eff, _bsc, s, e, kind, gap, rising = scored_pool[best_idx]
        chosen.append((s, e, kind, gap, rising))
        used_ms += (e - s)
        if used_ms >= budget_ms * 0.96:
            break
    chosen.sort(key=lambda c: c[0])
    return chosen


def suggest_intervals(
    lyrics: Lyrics,
    video_duration_ms: int,
    audio_samples: np.ndarray | None = None,
    sample_rate: int = 48000,
    audio_offset_ms: float = 0.0,
    range_ms: tuple[float, float] | None = None,
    treatment: CinematicTreatment | None = None,
    video_id: str = "",
    *,
    resource: VideoResource | None = None,
    opening_trim_ms: float | None = None,
    song_duration_ms: float | None = None,
    seed: int | None = None,
) -> list[CinematicInterval]:
    """Generate dynamic cinematic cuts synced to song structure, tempo, and contrast."""
    if not lyrics.lines and audio_samples is None:
        return []
    t_min, t_max = _range_bounds(lyrics, audio_samples, sample_rate, range_ms, song_duration_ms)
    span = t_max - t_min
    if span < 1800.0:
        return []

    analysis = analyze_audio(audio_samples, sample_rate) if audio_samples is not None else None
    fp = song_fingerprint(lyrics, analysis, t_min, t_max)
    resolved_seed = fp if seed is None else ((fp ^ ((int(seed) + 1) * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFFFFFF)
    rng = np.random.default_rng(resolved_seed)

    pacing, beat_ms = _estimate_pacing(lyrics, analysis, t_min, t_max)
    accents = [(t - audio_offset_ms, s) for t, s in (analysis.accents_ms if analysis else [])
               if t_min - 600.0 <= t - audio_offset_ms <= t_max + 600.0]
    beats = [t - audio_offset_ms for t in (analysis.beats_ms if analysis else [])
             if t_min - 400.0 <= t - audio_offset_ms <= t_max + 400.0]

    candidates: list[tuple[float, float, float, str, float, bool]] = []
    min_cand_dur = clamp(2100.0 - 650.0 * pacing, 1450.0, 2150.0)
    max_cand_dur = clamp(7800.0 - 1600.0 * pacing, 5600.0, 7800.0)
    base_gap_ms = clamp(4100.0 - 2100.0 * pacing, MIN_GAP_MS, 4600.0)

    def quantize_beats(target_ms: float, min_beats: int = 3, max_beats: int = 14) -> float:
        return int(clamp(round(target_ms / beat_ms), min_beats, max_beats)) * beat_ms

    def add_cand(score: float, s: float, e: float, kind: str, gap_scale: float = 1.0, rising: bool = False):
        s, e = clamp(s, t_min, t_max), clamp(e, t_min, t_max)
        if e - s > max_cand_dur:
            if kind == "interlude" and s <= t_min + 400.0:
                e = s + max_cand_dur
            else:
                mid = 0.5 * (s + e)
                s, e = mid - 0.5 * max_cand_dur, mid + 0.5 * max_cand_dur
        if e - s >= min_cand_dur:
            gap = clamp(base_gap_ms * gap_scale * float(rng.uniform(0.84, 1.22)), MIN_GAP_MS, 5800.0)
            candidates.append((score, s, e, kind, gap, rising))

    _collect_interlude_candidates(lyrics, t_min, t_max, beat_ms, max_cand_dur, rng, quantize_beats, add_cand)
    _collect_audio_candidates(analysis, t_min, t_max, audio_offset_ms, pacing, beat_ms, min_cand_dur,
                              accents, beats, rng, quantize_beats, add_cand)
    lines = _collect_lyric_candidates(lyrics, analysis, t_min, t_max, pacing, beat_ms,
                                      accents, beats, rng, quantize_beats, add_cand)

    if not candidates and analysis is None and span >= 2600.0:
        if lines and len(lines) >= 4 and span >= 14000.0:
            step = max(2, round(len(lines) / max(2.0, span / 11000.0)))
            offset_idx = int(rng.integers(0, min(step, len(lines))))
            for idx_ln in range(offset_idx, len(lines), step):
                ln = lines[idx_ln]
                w = quantize_beats(clamp(float(ln.end - ln.start) + 900.0, min_cand_dur, 4600.0), 3, 10)
                add_cand(1.2, float(ln.start) - 250.0, float(ln.start) - 250.0 + w, "accent", gap_scale=0.95)
        else:
            dur = min(DEFAULT_INTERVAL_MS, 0.52 * span * float(rng.uniform(0.88, 1.10)))
            mid = t_min + float(rng.uniform(0.42, 0.58)) * span
            add_cand(1.0, mid - 0.5 * dur, mid + 0.5 * dur, "accent")

    if not candidates:
        return []
    chosen = _select_candidates(candidates, span, pacing, min_cand_dur, rng)
    if not chosen:
        return []

    v_total = max(0.0, float(resource.duration_ms if (resource and resource.duration_ms > 0) else video_duration_ms))
    if resource is not None and resource.duration_ms > 0:
        v_first, v_last = resource.active_bounds_ms
        scene_cuts = [float(c) for c in resource.scene_cuts_ms if v_first <= c < v_last]
    else:
        eof_pad = 16.0 if v_total > 1500.0 else 0.0
        v_first, v_last = 0.0, max(0.0, v_total - eof_pad)
        scene_cuts = []

    inferred_song_end = max(
        t_max, float(song_duration_ms or 0.0),
        (analysis.duration_ms - audio_offset_ms) if (analysis is not None and analysis.duration_ms > 0) else 0.0,
        (float(max(l.end for l in lyrics.lines)) + 500.0) if lyrics.lines else 0.0,
    )
    song_t0 = 0.0 if t_min <= inferred_song_end else t_min
    song_span = max(span, inferred_song_end - song_t0)

    if opening_trim_ms is not None:
        open_ms = clamp(float(opening_trim_ms), v_first, max(v_first, v_last - MIN_INTERVAL_MS))
    else:
        open_ms = clamp(compute_opening_trim_ms(v_total, song_span, resource=resource), v_first, max(v_first, v_last - MIN_INTERVAL_MS))

    v_avail = max(0.0, v_last - open_ms)
    full_sync_mode = v_avail >= 0.65 * song_span
    base_tr = (copy.copy(treatment) if treatment else CinematicTreatment()).clamped()
    out: list[CinematicInterval] = []
    prev_vs: float | None = None

    for idx, (s, e, kind, _gap, rising) in enumerate(chosen):
        dur = _q10(e - s)
        e = _q10(s + dur)
        if v_total <= 0.0:
            vs, ve = 0.0, dur
        elif dur >= (v_last - v_first):
            vs, ve = _q10(v_first), _q10(max(v_first + 10.0, v_last))
        else:
            eff_open = open_ms if (v_last - open_ms) >= dur else max(v_first, v_last - dur)
            max_span_v = max(0.0, (v_last - eff_open) - dur)
            if full_sync_mode:
                u_song = clamp01((s - song_t0) / max(1.0, song_span - dur))
                jitter = float(rng.uniform(-1.0, 1.0)) * min(1350.0, 0.032 * max(1.0, v_avail))
                vs = clamp(eff_open + u_song * max_span_v + jitter, eff_open, v_last - dur)
                if prev_vs is not None:
                    vs = max(vs, min(v_last - dur, prev_vs + min(dur * 0.45, 500.0)))
            else:
                u_clip = clamp01((s - t_min) / max(1.0, span - dur))
                jitter = float(rng.uniform(-0.10, 0.10)) * max_span_v
                vs = clamp(eff_open + u_clip * max_span_v + jitter, eff_open, v_last - dur)
                if prev_vs is not None and prev_vs + 150.0 <= v_last - dur:
                    vs = max(vs, min(v_last - dur, prev_vs + 120.0))

            for cut_t in scene_cuts:
                if 25.0 < (cut_t - vs) < 380.0 and (cut_t + 40.0) <= (v_last - dur):
                    vs = cut_t + 40.0
                    break
            vs = _q10(clamp(vs, v_first, max(v_first, v_last - dur)))
            ve = _q10(min(v_total, vs + dur))
            if ve <= vs:
                vs = _q10(max(0.0, min(v_total - 20.0, vs)))
                ve = _q10(min(v_total, vs + min(dur, v_total - vs)))
            prev_vs = vs

        if kind in ("interlude", "pause"):
            fi_ratio, fo_ratio, fi_cap, fo_cap = float(rng.uniform(0.24, 0.33)), float(rng.uniform(0.27, 0.36)), 1350.0, 1500.0
        elif rising:
            fi_ratio, fo_ratio, fi_cap, fo_cap = float(rng.uniform(0.13, 0.21)), float(rng.uniform(0.22, 0.30)), 780.0, 1150.0
        else:
            fi_ratio, fo_ratio, fi_cap, fo_cap = float(rng.uniform(0.18, 0.26)), float(rng.uniform(0.22, 0.31)), 980.0, 1250.0

        fade_in = _q10(clamp(fi_ratio * dur, 260.0, fi_cap))
        fade_out = _q10(clamp(fo_ratio * dur, 320.0, fo_cap))
        if t_max - e <= 350.0:
            fade_out = _q10(clamp(max(fade_out, 0.32 * dur), 450.0, min(1600.0, 0.55 * dur)))

        out.append(CinematicInterval(
            id=f"iv_{idx + 1}", start_ms=round(s, 1), end_ms=round(e, 1),
            video_start_ms=round(vs, 1), video_end_ms=round(ve, 1),
            fade_in_ms=fade_in, fade_out_ms=fade_out,
            treatment=copy.copy(base_tr), video_id=video_id or (resource.id if resource else ""), kind=kind,
        ))
    return out
