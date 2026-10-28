"""Grabación de un tramo de canción de Spotify por loopback: play+seek, captura, alineación y recorte."""
import time

import numpy as np

from marea.audio import align
from marea.audio.loopback import AudioError, LoopbackRecorder
from marea.providers.spotify import SpotifyError

PREROLL_MS = 500  # se empieza a reproducir un poco antes del tramo: la latencia de arranque no se come el inicio
TAIL_S = 0.8  # y se graba un poco después del final
DENSE_S = 4.0  # primeras consultas de posición cada 0.25 s (para medir); luego una cada 2 s solo para vigilar
MIN_SAMPLES = 3
SILENCE_PEAK = 1e-3


class RecordingCancelled(Exception):
    pass


def record_song_range(client, track, start_ms: float, end_ms: float, *, device: str = "", offset_ms: float = 0.0,
                      preroll_ms: float = PREROLL_MS, fade_ms: float = 150.0, cancel=lambda: False,
                      progress=lambda fraction, text: None, recorder_factory=LoopbackRecorder,
                      clock=time.perf_counter, sleep=time.sleep) -> tuple[np.ndarray, int]:
    """(audio (n, canales) float32, frecuencia) exacto de [start_ms, end_ms] de `track`, con fundidos. Dura lo que dura el tramo.

    Levanta SpotifyError/AudioError con mensajes para el usuario, o RecordingCancelled si `cancel()` da True.
    """
    begin = max(0.0, start_ms - preroll_ms)
    total_s = (end_ms - begin) / 1000 + TAIL_S
    rec = recorder_factory(device)
    rec.start()  # primero la captura: así no se pierde nada de lo que suene tras el play
    samples, bad = [], 0
    try:
        client.play(track.uri, begin)
        t_play = clock()
        next_poll = t_play
        while True:
            now = clock()
            elapsed = now - t_play
            if cancel():
                raise RecordingCancelled()
            if elapsed >= total_s:
                break
            progress(elapsed / total_s, "Grabando audio…")
            if now >= next_poll:
                pb, sample = client.position_sample()
                if pb is not None and pb.track.id == track.id and pb.is_playing:
                    bad = 0
                    if sample and sample.progress_ms >= begin - 300:  # descarta lecturas viejas de antes del seek
                        samples.append(sample)
                else:
                    bad += 1
                    if bad >= 3 and elapsed > 3:
                        raise AudioError("La reproducción en Spotify se interrumpió mientras grabábamos. Evita pausar o cambiar de canción durante la exportación.")
                next_poll = now + (0.25 if elapsed < DENSE_S else 2.0)
            sleep(0.05)
    finally:
        try:
            client.pause()
        except SpotifyError:
            pass  # si falló el play o se perdió la red, este error taparía la causa real; la captura se cierra de todos modos
        finally:
            recording = rec.stop()
    if len(samples) < MIN_SAMPLES:
        raise AudioError("No pude medir la posición de Spotify para alinear el audio. Revisa tu conexión y vuelve a intentar.")
    try:
        c, _ = align.estimate_c(samples)
    except ValueError as e:
        raise AudioError(str(e)) from e
    segment = align.cut(recording, start_ms, end_ms, c, offset_ms, fade_ms)
    if not len(segment) or float(np.abs(segment).max()) < SILENCE_PEAK:
        raise AudioError("No se captó audio. Revisa que Spotify suene por el dispositivo elegido en Ajustes > Audio y que el volumen de Spotify no esté en cero.")
    progress(1.0, "Audio grabado")
    return segment, recording.rate
