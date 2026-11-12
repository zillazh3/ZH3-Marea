"""Análisis acústico de wordfx con audio sintético (sin archivos salvo tmp_path)."""
import hashlib
import os
import time

import numpy as np
import pytest

from marea.wordfx import audiofeat as af

RATE = 48000


def _t(dur, rate=RATE):
    return np.arange(int(dur * rate)) / rate


def _interp_env(dur, rate, per_s, rng, lo, hi):
    """Envolvente aleatoria (determinista) interpolada: `per_s` valores por segundo entre lo y hi."""
    pts = rng.uniform(lo, hi, int(dur * per_s) + 2)
    return np.interp(_t(dur, rate), np.arange(len(pts)) / per_s, pts)


def calm_voice(dur=12.0, rate=RATE, level=0.06):
    """Tono tipo voz tranquila: f0 200 Hz, 8 armónicos que decaen, envolvente lenta y suave."""
    t = _t(dur, rate)
    x = sum(np.sin(2 * np.pi * 200 * k * t) / k**1.5 for k in range(1, 9))
    env = 0.8 + 0.2 * np.sin(2 * np.pi * 0.25 * t)
    return (level * x * env).astype(np.float32)


def strained_voice(dur=12.0, rate=RATE, level=0.3, seed=1):
    """Entrega rota: f0 alto con armónicos agudos fuertes, ráfagas de ruido, picos fuertes y envolvente nerviosa."""
    rng = np.random.default_rng(seed)
    t = _t(dur, rate)
    f0 = 330 * (1 + 0.02 * np.sin(2 * np.pi * 6 * t))
    phase = 2 * np.pi * np.cumsum(f0) / rate
    x = sum(np.sin(k * phase) / k**0.4 for k in range(1, 18))
    x = x / np.abs(x).max()
    jitter = _interp_env(dur, rate, 25, rng, 0.4, 1.0)
    swell = 1 + 2.5 * np.exp(-(((t % 1.3) - 0.2) / 0.12) ** 2)  # un pico de volumen cada 1.3 s
    bursts = (np.sin(2 * np.pi * 3.1 * t) > 0.3) * rng.standard_normal(len(t)) * 0.7
    return (level * (x * jitter * swell + bursts)).astype(np.float32)


def pad(dur=12.0, rate=RATE):
    """Colchón grave: sub-bajo y acordes lentos sin nada de voz ni ruido."""
    t = _t(dur, rate)
    x = 0.4 * np.sin(2 * np.pi * 45 * t)
    for i, f in enumerate((110, 138.6, 164.8)):  # La mayor, tonos fundamentales
        x += 0.15 * np.sin(2 * np.pi * f * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.1 * t + i))
    return x.astype(np.float32)


def chords(progression, dur_each=3.0, rate=RATE):
    """Acordes con 4 armónicos por nota; `progression` = lista de listas de notas MIDI."""
    out = []
    for notes in progression:
        t = _t(dur_each, rate)
        env = np.minimum(1, np.minimum(t, dur_each - t) * 20)  # sin clics entre acordes
        x = sum(np.sin(2 * np.pi * 440 * 2 ** ((n - 69) / 12) * k * t) / k for n in notes for k in range(1, 5))
        out.append(0.1 * x * env)
    return np.concatenate(out).astype(np.float32)


C_MAJOR = [[60, 64, 67], [65, 69, 72], [67, 71, 74], [60, 64, 67]]  # C-E-G, F-A-C, G-B-D, C-E-G
A_MINOR = [[57, 60, 64], [62, 65, 69], [64, 68, 71], [57, 60, 64]]  # A-C-E, D-F-A, E-G#-B, A-C-E


def test_strain_calm_vs_strained_stem_path():
    a, ca = af.strain_curve(calm_voice(rate=16000), 16000, from_stem=True)
    b, cb = af.strain_curve(strained_voice(rate=16000), 16000, from_stem=True)
    assert af._global_strain(a) < 0.35
    assert af._global_strain(b) > 0.55


def test_strain_calm_vs_strained_mix_path():
    sa = af.analyze(calm_voice(), RATE)
    sb = af.analyze(strained_voice(), RATE)
    assert sa.strain < 0.35, sa.strain
    assert sb.strain > 0.55, sb.strain
    assert not sa.from_stem and not sb.from_stem


def test_strain_curve_shape_and_range():
    x = strained_voice(dur=7.3, rate=16000)
    curve, rate = af.strain_curve(x, 16000, from_stem=True)
    assert rate == 50.0
    assert curve.dtype == np.float32 and len(curve) == int(7.3 * 50)
    assert curve.min() >= 0.0 and curve.max() <= 1.0
    c2, _ = af.strain_curve(np.stack([x, x], axis=1), 16000)  # estéreo y tasa no entera por trama
    assert len(c2) == len(curve)
    assert len(af.strain_curve(np.zeros(5, np.float32), 16000)[0]) == 0


def test_stem_input_sets_from_stem():
    stats = af.analyze(calm_voice(dur=10), RATE, stem=strained_voice(dur=10, rate=16000), stem_rate=16000)
    assert stats.from_stem is True
    assert stats.strain > 0.55  # la tensión sale del stem, no de la mezcla tranquila
    assert len(stats.strain_curve) == 10 * 50 and stats.strain_rate == 50.0


def test_pad_not_strained_on_mix_path():
    stats = af.analyze(pad(), RATE)
    assert stats.strain < 0.2, stats.strain


def test_loud_instrumental_not_strained_on_mix_path():
    """Acordes fuertes + ruido de platillos suave: no hay voz forzada."""
    rng = np.random.default_rng(3)
    x = np.tile(chords(C_MAJOR, 3.0), 1) * 3
    hats = rng.standard_normal(len(x)).astype(np.float32) * 0.01 * (np.sin(2 * np.pi * 4 * _t(len(x) / RATE)) > 0)
    assert af.analyze(x + hats, RATE).strain < 0.45


def test_spectral_brightness():
    t = _t(6)
    assert af.spectral_brightness((0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32), RATE) < 0.3
    rng = np.random.default_rng(0)
    hi = sum(np.sin(2 * np.pi * f * t) for f in (6000, 8000, 10000, 12000)) + 0.1 * rng.standard_normal(len(t))
    assert af.spectral_brightness(hi.astype(np.float32), RATE) > 0.7


def test_key_and_mode_major():
    key, mode = af.key_and_mode(chords(C_MAJOR), RATE)
    assert key == 0 and mode > 0


def test_key_and_mode_minor():
    key, mode = af.key_and_mode(chords(A_MINOR), RATE)
    assert key == 9 and mode < 0


def test_key_unclear_on_silence_and_short():
    assert af.key_and_mode(np.zeros(RATE * 5, np.float32), RATE) == (-1, 0.0)
    assert af.key_and_mode(np.zeros(100, np.float32), RATE) == (-1, 0.0)


def test_analyze_end_to_end_and_deterministic():
    rng = np.random.default_rng(7)
    mono = chords(C_MAJOR * 2, 2.5) + 0.02 * rng.standard_normal(20 * RATE).astype(np.float32)
    stereo = np.stack([mono, mono * 0.8], axis=1).astype(np.float32)
    s1, s2 = af.analyze(stereo, RATE), af.analyze(stereo, RATE)
    assert s1.duration_ms == pytest.approx(20000.0)
    assert 0 <= s1.energy <= 1 and 0 <= s1.brightness <= 1 and 0 <= s1.strain <= 1
    assert -1 <= s1.mode <= 1 and -1 <= s1.key <= 11 and s1.tempo_bpm >= 0
    assert s1.strain_rate == 50.0 and len(s1.strain_curve) == 1000 and not s1.from_stem
    assert (s1.duration_ms, s1.tempo_bpm, s1.energy, s1.brightness, s1.mode, s1.key, s1.strain) == \
           (s2.duration_ms, s2.tempo_bpm, s2.energy, s2.brightness, s2.mode, s2.key, s2.strain)


def test_analyze_speed_four_minute_track():
    """Presupuesto ~1.5 s sin contar analyze_audio (se le pasa el análisis ya hecho)."""
    from marea.model.cinematic import analyze_audio
    x = np.tile(strained_voice(dur=12.0), 20)
    stereo = np.stack([x, x], axis=1).astype(np.float32)
    analysis = analyze_audio(stereo, RATE)
    t0 = time.perf_counter()
    af.analyze(stereo, RATE, analysis=analysis)
    assert time.perf_counter() - t0 < 4.0  # margen para CI lento


def _fake_audio(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(os.urandom(3 * 1024 * 1024 + 17))  # > 1 MB: ejercita la lectura por trozos
    sha = hashlib.sha256(audio.read_bytes()).hexdigest()
    d = tmp_path / "lab" / "cache" / sha
    d.mkdir(parents=True)
    return audio, tmp_path / "lab", d


def test_find_cached_stem_prefers_lead_and_newest(tmp_path):
    audio, root, d = _fake_audio(tmp_path)
    assert af.find_cached_stem(audio, root) is None  # sin nada
    np.save(d / "separate-a.npy", np.full(10, 0.1, np.float32))
    assert af.find_cached_stem(audio, root)[0] == pytest.approx(0.1)
    np.save(d / "lead-old.npy", np.full(10, 0.2, np.float32))
    np.save(d / "lead-new.npy", np.full(10, 0.3, np.float32))
    os.utime(d / "lead-old.npy", (1_000_000, 1_000_000))
    stem = af.find_cached_stem(audio, root)
    assert stem.dtype == np.float32 and stem.ndim == 1 and stem[0] == pytest.approx(0.3)


def test_find_cached_stem_corrupt_or_missing(tmp_path):
    audio, root, d = _fake_audio(tmp_path)
    (d / "lead-bad.npy").write_bytes(b"not a numpy file")
    assert af.find_cached_stem(audio, root) is None
    np.save(d / "separate-ok.npy", np.full(5, 0.5, np.float32))
    assert af.find_cached_stem(audio, root)[0] == pytest.approx(0.5)  # el lead corrupto se salta
    assert af.find_cached_stem(tmp_path / "missing.mp3", root) is None
