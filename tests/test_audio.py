import numpy as np
import pytest

from marea.audio import align, local, loopback
from marea.audio.align import PositionSample
from marea.audio.loopback import AudioError, Recording

RATE = 48000
CLICK_S = [10.0 + 0.5 * i for i in range(40)]  # clics de la «pista de prueba» a los 10.0, 10.5, 11.0… s de la canción


def click_track_recording(t0, play_start_local, p0_song):
    """Captura simulada: la canción suena desde `p0_song` (s) a partir del instante local `play_start_local`."""
    n = int(30 * RATE)
    x = np.zeros((n, 2), np.float32)
    for c in CLICK_S:
        i = round((play_start_local + (c - p0_song) - t0) * RATE)
        if 0 <= i < n - 40:
            x[i:i + 40, :] = 0.9
    return Recording(x, RATE, t0)


def api_samples(rng, play_start_local, p0_song, n=12, rtt_range=(0.03, 0.09), asym=0.2, noise_ms=10):
    """Consultas a Spotify: el servidor mide la posición real en algún punto del viaje, con ruido de milisegundos."""
    out = []
    for k in range(n):
        t_sent = play_start_local + 0.5 + k * 0.25
        rtt = rng.uniform(*rtt_range)
        t_measured = t_sent + rtt * (0.5 + rng.uniform(-asym, asym))
        progress = (p0_song + (t_measured - play_start_local)) * 1000 + rng.normal(0, noise_ms)
        out.append(PositionSample(progress, t_sent, t_sent + rtt))
    return out


@pytest.mark.parametrize("seed,latency", [(1, 0.12), (2, 0.35), (3, 0.0), (4, 0.2)])
def test_cut_lands_clicks_within_40ms(seed, latency):
    rng = np.random.default_rng(seed)
    p0, t0, play_local = 9.5, 100.0, 100.4 + latency  # se pidió la posición 9.5 s; el audio llegó con `latency` de retraso
    rec = click_track_recording(t0, play_local, p0)
    c, uncertainty = align.estimate_c(api_samples(rng, play_local, p0))
    start_ms, end_ms = 12_250, 16_250  # rango pedido (entre clics: un clic justo en el borde saldría o no según 1 ms)
    seg = align.cut(rec, start_ms, end_ms, c, fade_ms=0)
    detected = align.find_onsets(seg[:, 0], RATE)
    expected = [x - start_ms / 1000 for x in CLICK_S if start_ms / 1000 <= x < end_ms / 1000]
    assert len(detected) == len(expected)
    errors_ms = [abs(d - e) * 1000 for d, e in zip(detected, expected)]
    assert max(errors_ms) < 40, errors_ms
    assert uncertainty < 0.05


def test_high_rtt_samples_are_discarded_and_bad_networks_are_refused():
    good = PositionSample(5000, 10.00, 10.04)
    slow = PositionSample(9999, 11.00, 12.00)  # 1 s de viaje: no sirve
    assert align.estimate_c([good, slow])[0] == pytest.approx(5.0 - 10.02)
    with pytest.raises(ValueError, match="rápida"):
        align.estimate_c([slow])


def test_user_offset_shifts_the_cut_and_signal_outside_recording_is_silence():
    rec = Recording(np.ones((RATE * 2, 2), np.float32), RATE, 0.0)
    seg = align.cut(rec, 1000, 3000, c=0.0, fade_ms=0)  # pide hasta 3 s pero la grabación dura 2 s
    assert seg.shape == (2 * RATE, 2) and seg[:RATE].min() == 1 and seg[RATE:].max() == 0
    assert align.song_time_to_index(rec, 1.0, 0.0, offset_ms=100) == round(1.1 * RATE)


def test_fades_are_150ms_and_symmetric():
    out = align.apply_fades(np.ones((RATE, 2), np.float32), RATE, 150)
    n = round(0.15 * RATE)
    assert out[0, 0] == 0 and out[-1, 0] == 0 and out[n:-n].min() == 1 and 0 < out[n // 2, 0] < 1
    assert np.allclose(out[:n], out[::-1][:n])
    tiny = align.apply_fades(np.ones((100, 2), np.float32), RATE, 150)  # clip más corto que los fundidos: no truena
    assert tiny.shape == (100, 2)


def test_find_onsets_ignores_silence_and_ringing():
    x = np.zeros(RATE * 2, np.float32)
    x[RATE // 2:RATE // 2 + 2000] = np.hanning(2000)
    x[RATE:RATE + 2000] = np.hanning(2000)
    assert align.find_onsets(x, RATE) == pytest.approx([0.5 + 0.0, 1.0], abs=0.03)
    assert align.find_onsets(np.zeros(100, np.float32), RATE) == []


# ---------------- audio local ----------------

class FakeMic:
    """Grabadora falsa: `peak` es lo que «oyó»; `fail` simula un dispositivo que no se deja abrir."""
    opened = []

    def __init__(self, name, peak=0.0, fail=False):
        self.name, self.peak, self.fail = name, peak, fail

    def start(self):
        if self.fail:
            raise AudioError("ocupado")
        FakeMic.opened.append(self.name)

    def stop(self):
        FakeMic.opened.remove(self.name)
        n = np.zeros((100, 2), np.float32)
        n[10] = self.peak
        return Recording(n, 48000, 0.0)


def test_scan_levels_ranks_devices_skips_unopenable_and_leaves_none_open():
    FakeMic.opened = []
    devices = [{"name": "Altavoces", "loopback": True}, {"name": "Cable Input", "loopback": True},
               {"name": "Ocupado", "loopback": True}, {"name": "Mic", "loopback": False}]
    levels = {"Altavoces": 0.0, "Cable Input": 0.6, "Ocupado": 0.9, "Mic": 0.01}
    slept = []
    found = loopback.scan_levels(1.5, devices, lambda n: FakeMic(n, levels[n], fail=n == "Ocupado"), slept.append)
    assert [d["name"] for d in found] == ["Cable Input", "Mic", "Altavoces"]  # el ocupado no cuenta; el más fuerte primero
    assert found[0]["peak"] == pytest.approx(0.6) and found[0]["loopback"] is True
    assert slept == [1.5] and FakeMic.opened == []  # todos escuchan a la vez, durante el mismo tiempo, y se cierran


def test_scan_levels_closes_everything_even_if_the_wait_fails():
    FakeMic.opened = []

    def boom(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        loopback.scan_levels(1.5, [{"name": "A"}, {"name": "B"}], lambda n: FakeMic(n, 0.5), boom)
    assert FakeMic.opened == []


def test_wav_roundtrip_through_ffmpeg_decode(tmp_path):
    t = np.arange(RATE) / RATE
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path = tmp_path / "tono.wav"
    local.write_wav(path, np.stack([tone, tone], axis=1))
    back = local.decode(path)
    assert back.shape == (RATE, 2) and np.abs(back[:, 0] - tone).max() < 1e-3


def test_decode_invalid_file_has_a_plain_message(tmp_path):
    bad = tmp_path / "x.mp3"
    bad.write_bytes(b"esto no es audio")
    with pytest.raises(AudioError, match="No pude leer"):
        local.decode(bad)


def test_waveform_and_segment_with_offset():
    s = np.zeros((RATE * 4, 2), np.float32)
    s[RATE * 2:RATE * 2 + 100] = 0.8  # un golpe a los 2 s del archivo
    wf = local.waveform(s, 8)
    assert len(wf) == 8 and wf.argmax() == 4 and wf.max() == pytest.approx(0.8)
    # la letra pide 1.0–3.0 s; el archivo va 1 s «tarde» (offset +1000): el golpe cae al inicio del recorte
    seg = local.segment(s, 1000, 3000, offset_ms=1000, fade_ms=0)
    assert seg.shape == (2 * RATE, 2) and seg[:100].max() == pytest.approx(0.8) and seg[1000:].max() == 0
    assert local.segment(s, 3500, 5000, fade_ms=0)[RATE:].max() == 0  # más allá del archivo: silencio
    assert local.waveform(np.zeros((0, 2), np.float32), 4).tolist() == [0, 0, 0, 0]
