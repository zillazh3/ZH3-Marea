import numpy as np
import pytest

from marea.audio.loopback import GAP_S, assemble_chunks

RATE = 48000


def chunk(value, n=480):
    return np.full((n, 2), value, np.float32)


def test_contiguous_chunks_with_jitter_stay_contiguous():
    t = 10.0
    chunks, expected = [], []
    for k in range(20):
        jitter = (0.003 if k % 2 else -0.002)  # los callbacks no llegan con cadencia perfecta
        chunks.append((t + 0.01 * (k + 1) + jitter, chunk(k + 1)))
    rec = assemble_chunks(chunks, RATE, 2)
    assert len(rec.samples) == 20 * 480  # el jitter no inserta ni quita muestras
    assert rec.t0 == pytest.approx(10.0 + 0.01 - 0.002 - 0.01, abs=1e-6)


def test_silent_gap_without_packets_is_filled_so_audio_keeps_its_real_time():
    # WASAPI no manda bloques mientras todo está en silencio: el audio empieza 2 s después del primer bloque
    first = (100.0, chunk(0.0))  # un bloque de silencio al abrir el stream
    start_of_sound = 102.0
    sound = [(start_of_sound + 0.01 * (k + 1), chunk(0.5)) for k in range(10)]
    rec = assemble_chunks([first] + sound, RATE, 2)
    idx = int(np.argmax(rec.samples[:, 0] > 0.1))
    assert rec.time_of(idx) == pytest.approx(start_of_sound, abs=0.02)  # el sonido queda donde ocurrió
    assert rec.samples[:idx].max() == 0


def test_empty_recording_and_short_gaps_are_ignored():
    assert len(assemble_chunks([], RATE, 2, fallback_t0=5.0).samples) == 0
    a = (1.0, chunk(1.0))
    b = (1.0 + 0.01 + GAP_S * 0.5, chunk(2.0))  # hueco de 10 ms: jitter, no pausa
    assert len(assemble_chunks([a, b], RATE, 2).samples) == 960
