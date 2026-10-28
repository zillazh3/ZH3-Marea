import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtTest import QTest

from marea.audio import local
from marea.audio.loopback import AudioError
from marea.audio.session import RecordingCancelled
from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export import ffmpeg, history
from marea.export import queue as queue_mod
from marea.export.job import ExportJob
from marea.export.queue import AUDIO, CANCELLED, DONE, FAILED, QUEUED, RENDER, AudioSpec, ExportQueue, QueueItem
from marea.providers.spotify import Track
from marea.render.scene import Style

COVER = bytes(demo_cover(64).encodeToData())
TRACK = Track("tid", "Pista", ["X"], "", 200_000, None, "spotify:track:tid")


class FakeRunner(QObject):
    progress, finished, failed, cancelled = Signal(int, int, float), Signal(str), Signal(str, str), Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started, self.cancels = [], 0

    def start(self, job):
        self.started.append(job)

    def cancel(self):
        self.cancels += 1


def job(tmp_path, name="a.mp4", **kw):
    return ExportJob(exact_lyrics(), COVER, tmp_path / name, 12500, 13400, size=(180, 320), fps=30, prefer_gpu=False,
                     style=Style(title="x"), **kw)


def item(tmp_path, name="a.mp4", audio=None):
    return QueueItem("Título", "Artista", COVER, job(tmp_path, name), audio)


def pump(ms):
    """Espera con un bucle de eventos real. QTest.qWait no suelta el GIL y deja sin turno a los hilos que lanzan subprocess."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def until(cond, timeout=5000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        pump(20)
        if cond():
            return True
    return False


@pytest.fixture
def q(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(history, "make_thumbnail", lambda video: None)
    monkeypatch.setattr(history, "add", lambda entry, path=None: [entry])
    return ExportQueue(services=None, runner_factory=FakeRunner)


def test_jobs_run_one_at_a_time_in_order(q, tmp_path):
    a, b = item(tmp_path, "a.mp4"), item(tmp_path, "b.mp4")
    q.add(a)
    q.add(b)
    assert (a.state, b.state) == (RENDER, QUEUED) and len(q._runner.started) == 1 and q.busy
    q._runner.progress.emit(30, 60, 4.0)
    assert a.progress == 0.5 and a.eta == 4.0
    q._runner.finished.emit(str(a.out_path))
    assert a.state == DONE and a.progress == 1.0 and b.state == RENDER and q._runner.started[1] is b.job
    q._runner.finished.emit(str(b.out_path))
    assert b.state == DONE and not q.busy


def test_queued_cancel_removes_it_without_blocking_the_rest(q, tmp_path):
    a, b, c = (item(tmp_path, n) for n in ("a.mp4", "b.mp4", "c.mp4"))
    for i in (a, b, c):
        q.add(i)
    q.cancel(b)
    assert b.state == CANCELLED
    q._runner.finished.emit("x")
    assert c.state == RENDER
    q.cancel(c)
    assert q._runner.cancels == 1  # a un render en curso se le pide parar al proceso
    q._runner.cancelled.emit()
    assert c.state == CANCELLED and not q.busy


def test_render_failure_is_explained_and_next_job_continues(q, tmp_path):
    a, b = item(tmp_path, "a.mp4"), item(tmp_path, "b.mp4")
    q.add(a)
    q.add(b)
    q._runner.failed.emit("ffmpeg no pudo codificar", "traceback")
    assert a.state == FAILED and "ffmpeg" in a.error and "espacio" in a.error and b.state == RENDER
    q.remove(b)  # un trabajo activo no se puede quitar
    assert b in q.items
    q._runner.cancelled.emit()
    q.clear_finished()
    assert q.items == []


def test_local_audio_stage_creates_wav_then_renders_and_cleans_up(q, tmp_path):
    samples = np.full((local.RATE * 3, 2), 0.3, np.float32)
    it = item(tmp_path, audio=AudioSpec("local", 12500, 13400, samples=samples, offset_ms=0))
    q.add(it)
    assert it.state == AUDIO
    assert until(lambda: it.state == RENDER)
    wav = it.job.audio_path
    assert wav and wav.exists() and local.decode(wav).shape[0] == pytest.approx(0.9 * local.RATE, abs=2)
    q._runner.finished.emit(str(it.out_path))
    assert it.state == DONE and not wav.exists() and it.wav is None  # el temporal se borra


def test_fade_out_ramps_the_outro_to_silence(q, tmp_path):
    samples = np.full((local.RATE * 3, 2), 0.5, np.float32)
    it = item(tmp_path, audio=AudioSpec("local", 0, 2000, samples=samples, fade_out_ms=1000))
    q.add(it)
    assert until(lambda: it.state == RENDER)
    out = local.decode(it.job.audio_path)
    assert abs(out[-1]).max() < 0.01 and out[len(out) // 2 - 2000, 0] > 0.4  # antes del outro, intacto
    assert out[-local.RATE // 2, 0] == pytest.approx(0.25, abs=0.05)  # a mitad del outro, la mitad
    q._runner.finished.emit(str(it.out_path))


def test_spotify_audio_stage_reports_progress_and_uses_settings(q, tmp_path, monkeypatch):
    calls = {}

    def fake_record(client, track, start, end, *, device, offset_ms, cancel, progress, **kw):
        calls.update(track=track, start=start, end=end, device=device, offset=offset_ms)
        progress(0.4, "Grabando audio…")
        QTest.qWait(40)
        return np.full((int(0.9 * 44100), 2), 0.2, np.float32), 44100

    monkeypatch.setattr(queue_mod, "record_song_range", fake_record)
    q.services = type("S", (), {"spotify": object()})()
    it = item(tmp_path, audio=AudioSpec("spotify", 12500, 13400, track=TRACK, offset_ms=-60, device="Mi dispositivo"))
    q.add(it)
    assert until(lambda: it.state == RENDER)
    assert calls == {"track": TRACK, "start": 12500, "end": 13400, "device": "Mi dispositivo", "offset": -60}
    with __import__("wave").open(str(it.job.audio_path)) as w:
        assert w.getframerate() == 44100  # el WAV conserva la frecuencia real de la captura


def test_audio_failure_and_cancel_in_audio_stage(q, tmp_path, monkeypatch):
    def failing(*a, **k):
        raise AudioError("No se captó audio. Revisa el dispositivo.")

    monkeypatch.setattr(queue_mod, "record_song_range", failing)
    q.services = type("S", (), {"spotify": object()})()
    bad, ok = item(tmp_path, "bad.mp4", AudioSpec("spotify", 0, 1000, track=TRACK)), item(tmp_path, "ok.mp4")
    q.add(bad)
    q.add(ok)
    assert until(lambda: bad.state == FAILED)
    assert bad.error == "No se captó audio. Revisa el dispositivo." and ok.state == RENDER  # sigue con el siguiente
    q._runner.finished.emit("x")

    def waits_for_cancel(client, track, start, end, *, cancel, **kw):
        while not cancel():
            time.sleep(0.01)
        raise RecordingCancelled()

    monkeypatch.setattr(queue_mod, "record_song_range", waits_for_cancel)
    c = item(tmp_path, "c.mp4", AudioSpec("spotify", 0, 1000, track=TRACK))
    q.add(c)
    assert c.state == AUDIO
    q.cancel(c)
    assert until(lambda: c.state == CANCELLED) and not q.busy


def test_unexpected_audio_error_does_not_leak_internals(q, tmp_path, monkeypatch):
    monkeypatch.setattr(queue_mod, "record_song_range", lambda *a, **k: 1 / 0)
    q.services = type("S", (), {"spotify": object()})()
    it = item(tmp_path, audio=AudioSpec("spotify", 0, 1000, track=TRACK))
    q.add(it)
    assert until(lambda: it.state == FAILED) and "division" not in it.error and "audio" in it.error


def test_history_and_item_done_signal(qapp, monkeypatch, tmp_path):
    saved, thumbs = [], tmp_path / "t.png"
    thumbs.write_bytes(b"x")
    monkeypatch.setattr(history, "make_thumbnail", lambda video: thumbs)
    monkeypatch.setattr(history, "add", lambda entry, path=None: saved.append(entry) or [entry])
    q = ExportQueue(None, runner_factory=FakeRunner)
    done = []
    q.item_done.connect(done.append)
    it = item(tmp_path)
    q.add(it)
    q._runner.finished.emit(str(it.out_path))
    assert until(lambda: bool(done))
    assert saved[0]["title"] == "Título" and saved[0]["thumb"] == str(thumbs) and it.thumb == thumbs


def test_history_file_roundtrip_drops_missing_videos(tmp_path):
    f = tmp_path / "h.json"
    real = tmp_path / "v.mp4"
    real.write_bytes(b"x")
    history.add({"path": str(real), "title": "A", "artist": "", "thumb": ""}, f)
    history.add({"path": str(tmp_path / "borrado.mp4"), "title": "B", "artist": "", "thumb": ""}, f)
    assert [e["title"] for e in history.load(f)] == ["A"]  # el video borrado ya no aparece
    history.add({"path": str(real), "title": "A2", "artist": "", "thumb": ""}, f)
    assert [e["title"] for e in history.load(f)] == ["A2"]  # reexportar el mismo archivo no duplica
    f.write_text("no es json")
    assert history.load(f) == []


def test_real_pipeline_local_audio_to_mp4_with_aac(qapp, tmp_path, monkeypatch):
    """De punta a punta: audio local -> WAV recortado -> render en proceso aparte -> mp4 con video y audio."""
    monkeypatch.setattr(history, "add", lambda entry, path=None: [entry])
    q = ExportQueue(None)  # runner real (proceso aparte)
    t = np.arange(local.RATE * 3) / local.RATE
    tone = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    it = item(tmp_path, audio=AudioSpec("local", 12500, 13400, samples=np.stack([tone, tone], axis=1)))
    q.add(it)
    assert until(lambda: it.state in (DONE, FAILED), 90_000), it.state
    assert it.state == DONE, it.error
    info = json.loads(subprocess.run([ffmpeg.find_ffmpeg("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                      str(it.out_path)], capture_output=True, text=True).stdout)
    assert sorted(s["codec_name"] for s in info["streams"]) == ["aac", "h264"]
    assert float(info["format"]["duration"]) == pytest.approx(0.9, abs=0.1)
    assert it.wav is None
