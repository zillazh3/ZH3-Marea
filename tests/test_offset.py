"""Desfase automático: audio sintético hecho con los tiempos de la canción demo (ráfagas en cada inicio de sílaba, más bombo
regular, golpes al azar y ruido). Calibración con 180 pruebas: 0 falsos positivos y error ≤ 5 ms en lo aceptado."""
import copy

import numpy as np
import pytest

from marea.audio import align, local
from marea.demo_song import exact_lyrics
from marea.model.lyrics import Line, Lyrics, spread

RATE = 48000
LY = exact_lyrics()
END = LY.lines[-1].end


def synth(lyrics, shift_ms, rate=RATE, seed=1, decoys=True, noise=0.03):
    rng = np.random.default_rng(seed)
    dur = lyrics.lines[-1].end / 1000 + 2
    n = int(dur * rate)
    x = rng.normal(0, noise, n).astype(np.float32)
    t = np.arange(int(0.12 * rate)) / rate
    for line in lyrics.lines:
        for i, s in enumerate(line.syllables):
            if line.approx and i:
                continue  # el audio de una canción real solo tiene ataque donde se canta, no en tiempos inventados
            f = rng.uniform(300, 2500)
            burst = np.sin(2 * np.pi * f * t) * np.exp(-t / 0.05) * rng.uniform(0.3, 0.9)
            burst += rng.normal(0, 0.05, len(t)) * np.exp(-t / 0.03)
            k = int((s.begin + shift_ms) / 1000 * rate)
            if 0 <= k and k + len(burst) < n:
                x[k:k + len(burst)] += burst.astype(np.float32)
    if decoys:  # bombo a 120 BPM y golpes sin relación con la letra
        kick = (np.sin(2 * np.pi * 90 * t) * np.exp(-t / 0.04)).astype(np.float32)
        for k in np.arange(0, dur - 0.2, 0.5):
            i = int(k * rate)
            x[i:i + len(kick)] += 0.6 * kick
        for k in rng.uniform(0, dur - 0.2, int(dur * 2)):
            i = int(k * rate)
            x[i:i + len(t)] += (rng.uniform(0.2, 0.6) * np.sin(2 * np.pi * rng.uniform(300, 3000) * t) * np.exp(-t / 0.05)).astype(np.float32)
    return x


def shuffled(seed=5):
    rng = np.random.default_rng(seed)
    other = copy.deepcopy(LY)
    for line in other.lines:
        for s in line.syllables:
            s.begin = int(rng.uniform(0, END))
    return other


@pytest.mark.parametrize("shift", [0, 230, -310, 800, -1200, 55])
def test_recovers_the_shift_within_a_few_ms(shift):
    d = align.detect_offset(synth(LY, shift), RATE, LY)
    assert d.offset_ms == pytest.approx(shift, abs=8) and d.reliable and not d.at_edge


def test_sign_matches_local_segment_so_applying_the_result_realigns_the_audio():
    """offset > 0 = el audio llega más tarde que la letra: segment() lee más adelante en el archivo y los ataques vuelven a su sitio."""
    audio = synth(LY, 230, decoys=False, noise=0.005)
    samples = np.stack([audio, audio], axis=1)
    d = align.detect_offset(samples, RATE, LY)
    start, end = 20000, 26000
    seg = local.segment(samples, start, end, d.offset_ms, fade_ms=0)
    got = align.find_onsets(seg[:, 0], RATE, threshold=0.35, min_gap_s=0.08)
    want = [(s.begin - start) / 1000 for l in LY.lines for s in l.syllables if start <= s.begin < end]
    near = [min(abs(g - w) for w in want) for g in got]
    assert got and max(near) < 0.02  # cada ataque queda a menos de 20 ms de su sílaba
    unfixed = local.segment(samples, start, end, 0.0, fade_ms=0)
    off = align.find_onsets(unfixed[:, 0], RATE, threshold=0.35, min_gap_s=0.08)
    assert np.median([min(abs(o - w) for w in want) for o in off]) > 0.1  # sin corregir, la mayoría queda ~230 ms tarde


def test_short_clip_of_a_recording_uses_start_ms_as_the_origin_of_time():
    start = 17000
    seg = synth(LY, 150)[int(start / 1000 * RATE):int((start + 20000) / 1000 * RATE)]
    d = align.detect_offset(seg, RATE, LY, start_ms=start, max_lag_ms=500)
    assert d.offset_ms == pytest.approx(150, abs=8) and d.reliable
    wrong = align.detect_offset(seg, RATE, LY, start_ms=0, max_lag_ms=500)  # con el origen equivocado no hay nada que alinear
    assert not wrong.reliable


def test_other_formats_stereo_44k_are_handled():
    x = synth(LY, -150, rate=44100)
    d = align.detect_offset(np.stack([x, 0.8 * x], axis=1), 44100, LY)
    assert d.offset_ms == pytest.approx(-150, abs=8) and d.reliable


def test_unrelated_audio_is_not_trusted():
    d = align.detect_offset(synth(LY, 0, seed=9), RATE, shuffled())
    assert not d.reliable


def test_offset_outside_the_search_range_is_flagged_instead_of_guessed():
    d = align.detect_offset(synth(LY, 1800), RATE, LY, max_lag_ms=500)
    assert not d.reliable  # el pico real está fuera: lo que se ve en ±500 ms es ruido o el borde


def test_line_synced_lyrics_only_use_line_starts():
    lines = [Line(2000 + 2500 * i, 4000 + 2500 * i, syllables=spread("una línea de prueba larga", 2000 + 2500 * i, 4000 + 2500 * i), approx=True)
             for i in range(30)]
    ly = Lyrics(lines)
    d = align.detect_offset(synth(ly, 310, decoys=True), RATE, ly)
    assert d.offset_ms == pytest.approx(310, abs=10) and d.reliable


def test_silence_and_too_little_lyrics_give_clear_errors():
    with pytest.raises(ValueError, match="silencio"):
        align.detect_offset(np.zeros(RATE * 5, np.float32), RATE, LY)
    few = Lyrics([Line(1000, 2000, syllables=spread("solo tres palabras", 1000, 2000))])
    with pytest.raises(ValueError, match="muy pocas"):
        align.detect_offset(synth(LY, 0), RATE, few)
    with pytest.raises(ValueError, match="silencio"):
        align.detect_offset(np.zeros(100, np.float32), RATE, LY)  # menos de una ventana
