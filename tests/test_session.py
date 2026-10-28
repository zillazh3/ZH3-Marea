"""Grabación por loopback en un mundo simulado: reloj virtual, Spotify falso y captura sintética con una pista de clics."""
import numpy as np
import pytest

from marea.audio import align
from marea.audio.align import PositionSample
from marea.audio.loopback import AudioError, Recording
from marea.audio.session import RecordingCancelled, record_song_range
from marea.providers.spotify import Playback, SpotifyError, Track

RATE = 48000
CLICKS = [5.0 + 0.5 * i for i in range(400)]  # clics de la pista de prueba, cada 0.5 s desde los 5 s
TRACK = Track("tid", "Pista", ["X"], "", 200_000, None, "spotify:track:tid")


class World:
    def __init__(self, audio_latency=0.18, rtt=0.06, silent=False, stop_after=None, sample_rtt=None):
        self.now, self.log = 100.0, []
        self.audio_latency, self.rtt, self.silent, self.stop_after, self.sample_rtt = audio_latency, rtt, silent, stop_after, sample_rtt
        self.play_local = self.p0 = None
        self.rng = np.random.default_rng(7)
        self.recorder_t0 = None

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def true_position(self, t_local):
        return self.p0 + (t_local - self.play_local)


class FakeRecorder:
    def __init__(self, world):
        self.w = world

    def start(self):
        self.w.log.append("rec.start")
        self.w.recorder_t0 = self.w.now

    def stop(self):
        w = self.w
        w.log.append("rec.stop")
        n = int((w.now - w.recorder_t0) * RATE)
        x = np.zeros((n, 2), np.float32)
        if not w.silent and w.play_local is not None:
            for c in CLICKS:
                i = round((w.play_local + (c - w.p0) - w.recorder_t0) * RATE)
                if 0 <= i < n - 40:
                    x[i:i + 40] = 0.9
        return Recording(x, RATE, w.recorder_t0)


class FakeClient:
    def __init__(self, world):
        self.w, self.polls = world, 0

    def play(self, uri, position_ms):
        self.w.log.append(f"play {uri} @{position_ms:.0f}")
        self.w.p0 = position_ms / 1000
        self.w.play_local = self.w.now + self.w.audio_latency

    def pause(self):
        self.w.log.append("pause")

    def position_sample(self):
        w = self.w
        self.polls += 1
        t_sent, rtt = w.now, w.sample_rtt or w.rtt * w.rng.uniform(0.6, 1.4)
        w.now += rtt
        playing = w.stop_after is None or self.polls <= w.stop_after
        progress = (w.true_position(t_sent + rtt * 0.5) * 1000 + w.rng.normal(0, 8)) if playing else 0
        pb = Playback(TRACK, int(progress), playing, 0)
        return pb, (PositionSample(progress, t_sent, w.now) if playing else None)


def run(world, start_ms=60_000, end_ms=64_000, **kw):
    seg, rate = record_song_range(FakeClient(world), TRACK, start_ms, end_ms, recorder_factory=lambda dev: FakeRecorder(world),
                                  clock=world.clock, sleep=world.sleep, **kw)
    assert rate == RATE
    return seg


def test_recording_aligns_clicks_within_40ms_and_orders_calls():
    w = World()
    seg = run(w, start_ms=60_250, end_ms=64_250, fade_ms=0)  # entre clics: uno justo en el borde saldría o no por 1 ms
    assert w.log[0] == "rec.start" and w.log[1].startswith("play spotify:track:tid @59750") and w.log[-2:] == ["pause", "rec.stop"]
    assert len(seg) == 4 * RATE  # exactamente el tramo pedido
    detected = align.find_onsets(seg[:, 0], RATE)
    expected = [c - 60.25 for c in CLICKS if 60.25 <= c < 64.25]
    assert len(detected) == len(expected) == 8
    errors = [abs(d - e) * 1000 for d, e in zip(detected, expected)]
    assert max(errors) < 40, errors


@pytest.mark.parametrize("latency", [0.0, 0.1, 0.3, 0.45])
def test_alignment_holds_for_different_output_latencies(latency):
    seg = run(World(audio_latency=latency), start_ms=61_250, end_ms=65_250, fade_ms=0)
    detected = align.find_onsets(seg[:, 0], RATE)
    expected = [c - 61.25 for c in CLICKS if 61.25 <= c < 65.25]
    assert len(detected) == len(expected) and max(abs(d - e) for d, e in zip(detected, expected)) < 0.04


def test_user_offset_compensates_a_systematic_delay():
    w = World()
    seg = run(w, start_ms=61_250, end_ms=65_250, fade_ms=0, offset_ms=-100)  # el usuario sabe que el audio sobra 100 ms
    detected = align.find_onsets(seg[:, 0], RATE)
    expected = [c - 61.25 for c in CLICKS if 61.25 <= c < 65.25]
    assert abs(detected[0] - expected[0] - (-0.1)) < 0.04 or abs(detected[0] - expected[0] - 0.1) < 0.04  # se desplaza ±100 ms


def test_fades_and_progress_reporting():
    w, seen = World(), []
    seg = run(w, progress=lambda f, text: seen.append(f))
    assert seg[0, 0] == 0 and seg[-1, 0] == 0 and seen[-1] == 1.0 and seen == sorted(seen) and seen[0] < 0.1


def test_cancel_pauses_playback_and_stops_capture():
    w = World()
    n = []
    with pytest.raises(RecordingCancelled):
        run(w, cancel=lambda: (n.append(1), len(n) > 40)[1])
    assert w.log[-2:] == ["pause", "rec.stop"]


def test_interrupted_playback_is_reported():
    with pytest.raises(AudioError, match="se interrumpió"):
        run(World(stop_after=16), start_ms=60_000, end_ms=75_000)  # tras la fase de medición, el usuario pausa


def test_silence_and_slow_network_are_reported_in_plain_words():
    with pytest.raises(AudioError, match="No se captó audio"):
        run(World(silent=True))
    with pytest.raises(AudioError, match="posición"):
        run(World(sample_rtt=0.9))  # todas las mediciones tardaron casi 1 s: no sirven para alinear


def test_spotify_errors_from_play_propagate_and_still_stop_capture():
    w = World()

    class NoPremium(FakeClient):
        def play(self, uri, position_ms):
            raise SpotifyError("Controlar la reproducción requiere Spotify Premium.")

    with pytest.raises(SpotifyError, match="Premium"):
        record_song_range(NoPremium(w), TRACK, 60_000, 64_000, recorder_factory=lambda d: FakeRecorder(w),
                          clock=w.clock, sleep=w.sleep)
    assert w.log == ["rec.start", "rec.stop"] or w.log[-1] == "rec.stop"


def test_a_failing_pause_neither_hides_the_real_error_nor_leaves_the_capture_open():
    w = World()

    class Broken(FakeClient):
        def play(self, uri, position_ms):
            raise SpotifyError("Controlar la reproducción requiere Spotify Premium.")

        def pause(self):
            w.log.append("pause")
            raise SpotifyError("Spotify no respondió.")

    with pytest.raises(SpotifyError, match="Premium"):  # el error del play, no el del pause
        record_song_range(Broken(w), TRACK, 60_000, 64_000, recorder_factory=lambda d: FakeRecorder(w),
                          clock=w.clock, sleep=w.sleep)
    assert w.log == ["rec.start", "pause", "rec.stop"]
