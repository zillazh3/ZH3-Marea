"""Reproductor de la revisión. Sin dispositivo de salida se salta (con motivo); lo demás se prueba sin sonar."""
import numpy as np
import pytest

from marea.audio.local import RATE
from marea.lab.player import Player
from tests.test_spotify_ui import until


def silence(seconds: float) -> np.ndarray:
    return np.zeros((round(seconds * RATE), 2), np.float32)


def test_an_empty_or_inverted_range_plays_nothing(qapp):
    p = Player()
    p.play(silence(1.0), 500, 500)
    p.play(silence(1.0), 900, 200)
    assert not p.playing
    p.close()


def test_play_reports_position_from_the_start_of_the_range_and_finishes(qapp):
    p = Player()
    seen, done = [], []
    p.position.connect(seen.append)
    p.finished.connect(lambda: done.append(True))
    try:
        p.play(silence(2.0), 1000, 1400)
    except OSError:
        pytest.skip("Este equipo no tiene dispositivo de salida de audio.")
    assert p.playing
    assert until(lambda: done, 5000), "la reproducción de 0.4 s no terminó"
    assert seen and 1000 <= min(seen) and max(seen) <= 1000 + 400 + 200  # posiciones dentro del tramo (con holgura del buffer)
    assert not p.playing
    p.close()


def test_stop_cancels_and_a_new_play_replaces_the_previous_one(qapp):
    p = Player()
    try:
        p.play(silence(5.0), 0)
    except OSError:
        pytest.skip("Este equipo no tiene dispositivo de salida de audio.")
    first = p._stream
    p.play(silence(5.0), 2000)
    assert p.playing and p._stream is not first and p.position_ms >= 2000
    p.stop()
    assert not p.playing
    p.close()
