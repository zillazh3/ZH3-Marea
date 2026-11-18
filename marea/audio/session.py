"""Grabación de un tramo de canción de Spotify por loopback: play+seek, captura, alineación y recorte."""
import time

import numpy as np

from marea.audio import align
from marea.audio.loop import loop_pcm
from marea.audio.loopback import AudioError, LoopbackRecorder
from marea.i18n import t
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
                      clock=time.perf_counter, sleep=time.sleep,
                      loop_fade_ms: float | None = None) -> tuple[np.ndarray, int]:
    """(audio (n, canales) float32, frecuencia) exacto de [start_ms, end_ms] de `track`, con fundidos. Dura lo que dura el tramo.

    Levanta SpotifyError/AudioError con mensajes para el usuario, o RecordingCancelled si `cancel()` da True.

    `loop_fade_ms` captures extra handles and replaces legacy fades with a fixed-duration seam mix.
    """
    handle_ms = min(max(0.0, loop_fade_ms or 0.0), (end_ms - start_ms) / 2) / 2
    begin = max(0.0, start_ms - preroll_ms - handle_ms + (min(offset_ms, 0.0) if loop_fade_ms is not None else 0.0))
    total_s = (end_ms - begin + handle_ms + (max(offset_ms, 0.0) if loop_fade_ms is not None else 0.0)) / 1000 + TAIL_S
    rec = recorder_factory(device)
    rec.start()  # primero la captura: así no se pierde nada de lo que suene tras el play
    samples, bad, aborted = [], 0, True
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
            progress(elapsed / total_s, t("errors.audio.recording"))
            if now >= next_poll:
                try:
                    pb, sample = client.position_sample()
                except SpotifyError:
                    bad += 1
                    if bad >= 3 and elapsed > 3:
                        raise
                    next_poll = now + (0.25 if elapsed < DENSE_S else 2.0)
                    sleep(0.05)
                    continue
                if pb is not None and pb.track.id == track.id and pb.is_playing:
                    bad = 0
                    if sample and sample.progress_ms >= begin - 300:  # descarta lecturas viejas de antes del seek
                        samples.append(sample)
                else:
                    bad += 1
                    if bad >= 3 and elapsed > 3:
                        raise AudioError(t("errors.audio.spotifyInterrupted"))
                next_poll = now + (0.25 if elapsed < DENSE_S else 2.0)
            sleep(0.05)
        aborted = False
    finally:
        try:
            client.pause()
        except SpotifyError:
            pass  # si falló el play o se perdió la red, este error taparía la causa real; la captura se cierra de todos modos
        finally:
            if aborted and isinstance(rec, LoopbackRecorder):
                recording = rec.stop(assemble=False)
            else:
                recording = rec.stop()
    if len(samples) < MIN_SAMPLES:
        raise AudioError(t("errors.audio.noPosition"))
    try:
        c, _ = align.estimate_c(samples)
    except ValueError as e:
        raise AudioError(str(e)) from e
    if loop_fade_ms is None:
        segment = align.cut(recording, start_ms, end_ms, c, offset_ms, fade_ms)
    else:
        start = align.song_time_to_index(recording, start_ms / 1000, c, offset_ms)
        length = round((end_ms - start_ms) * recording.rate / 1000)
        segment = loop_pcm(recording.samples, recording.rate, start, length, loop_fade_ms)
    if not len(segment) or float(np.abs(segment).max()) < SILENCE_PEAK:
        raise AudioError(t("errors.audio.nothingCaptured"))
    progress(1.0, t("errors.audio.recorded"))
    return segment, recording.rate
