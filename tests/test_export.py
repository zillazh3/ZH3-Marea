import hashlib
import json
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest
import skia
from PySide6.QtCore import QEventLoop, QTimer

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export import ffmpeg
from marea.export.job import (ExportCancelled, ExportJob, clip_name, clip_range, render_frames, run_export,
                              unique_path)
from marea.export.runner import ExportRunner
from marea.render.scene import Style

SIZE, FPS = (180, 320), 30


def cover_png() -> bytes:
    return bytes(demo_cover().encodeToData())  # PNG


def job(tmp_path, start=12500, frames=12, **kw):
    kw.setdefault("style", Style(title="Marea de luz", artist="Demo"))
    return ExportJob(exact_lyrics(), cover_png(), tmp_path / "clip.mp4", start, start + frames / FPS * 1000,
                     size=SIZE, fps=FPS, prefer_gpu=False, **kw)


def probe(path: Path) -> dict:
    out = subprocess.run([ffmpeg.find_ffmpeg("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-show_format",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def digest(frames) -> list[str]:
    return [hashlib.sha256(f.tobytes()).hexdigest() for f in frames]


def test_clip_name_and_unique_path(tmp_path):
    assert clip_name("Artista", "Canción", 2, 6) == "Artista - Canción (versos 3–7) [ZH3 Marea].mp4"
    assert clip_name("", "Solo título", 0, 0) == "Solo título (versos 1–1) [ZH3 Marea].mp4"
    bad = clip_name('AC/DC', 'Who? "Made": Who*', 0, 1)
    assert not any(c in bad for c in '/\\:*?"<>|') and bad.endswith("(versos 1–2) [ZH3 Marea].mp4")
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "a (2).mp4").write_bytes(b"x")
    assert unique_path(tmp_path, "a.mp4").name == "a (3).mp4" and unique_path(tmp_path, "b.mp4").name == "b.mp4"


def test_clip_range_covers_chosen_verses_plus_tail():
    ly = exact_lyrics()
    start, end = clip_range(ly, 2, 3)  # 3.ª línea (tiene voz de fondo hasta 17.8 s) y 4.ª (hasta 21.1 s)
    assert start == pytest.approx(13400) and end == pytest.approx(21100 + 500)
    assert clip_range(ly, 0, 0)[0] == pytest.approx(6000)
    assert clip_range(ly, 2, 3, tail_ms=4000)[1] == pytest.approx(21100 + 4000)


def test_ffmpeg_command_defaults_and_variants(tmp_path):
    cmd = ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60)
    joined = " ".join(cmd)
    for part in ("-f rawvideo -pix_fmt rgba -s 1080x1920 -r 60 -i -", "libx264 -preset slow -crf 16", "yuv420p",
                 "+faststart", "bt709"):
        assert part in joined
    assert "-c:a" not in cmd  # "solo letra": sin audio
    nv = ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60, "nvenc")
    assert "h264_nvenc" in nv and "libx264" not in nv
    beep = ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60, beep=True)
    assert "sine=frequency=1000" in " ".join(beep) and "aac" in beep and "320k" in beep


def test_frames_are_identical_across_runs_and_change_over_time(tmp_path):
    a, b = digest(render_frames(job(tmp_path))), digest(render_frames(job(tmp_path)))
    assert a == b and len(a) == 12
    assert len(set(a)) > 6  # la letra se anima: los frames no son todos iguales


def test_start_marker_flash_is_one_white_frame(tmp_path):
    frames = list(render_frames(job(tmp_path, marker="flash", frames=4)))
    assert (frames[0] == 255).all()
    assert not (frames[1] == 255).all()


def test_encodes_valid_h264_and_leaves_no_audio(tmp_path):
    out = run_export(job(tmp_path, frames=30))
    info = probe(out)
    (v,) = info["streams"]
    assert (v["codec_name"], v["pix_fmt"], v["width"], v["height"]) == ("h264", "yuv420p", 180, 320)
    assert v["nb_read_frames"] == "30" and v["color_space"] == "bt709"
    assert float(info["format"]["duration"]) == pytest.approx(1.0, abs=0.05)
    raw = out.read_bytes()
    assert raw.find(b"moov") < raw.find(b"mdat")  # +faststart: reproducible antes de terminar de descargar


def test_beep_marker_adds_aac_track(tmp_path):
    info = probe(run_export(job(tmp_path, frames=30, marker="beep")))
    assert sorted(s["codec_name"] for s in info["streams"]) == ["aac", "h264"]


@pytest.mark.skipif(not ffmpeg.nvenc_available(), reason="NVENC no disponible")
def test_nvenc_encodes(tmp_path):
    info = probe(run_export(job(tmp_path, frames=15, encoder="nvenc")))
    assert info["streams"][0]["codec_name"] == "h264"


def test_progress_and_cancel_leave_no_partial_file(tmp_path):
    seen = []
    j = job(tmp_path, frames=20)
    with pytest.raises(ExportCancelled):
        run_export(j, progress=lambda d, n, eta: seen.append((d, n)), cancelled=lambda: len(seen) >= 1)
    assert not j.out_path.exists()
    seen.clear()
    run_export(j, progress=lambda d, n, eta: seen.append((d, n)))
    assert seen[-1] == (20, 20) and j.out_path.exists()


def test_ffmpeg_failure_is_reported_in_plain_words(tmp_path):
    j = job(tmp_path, frames=3)
    j.out_path = tmp_path / "no_existe" / "x" / "clip.mp4"
    j.out_path.parent.mkdir(parents=True)
    j.out_path.parent.rmdir()  # la carpeta se crea sola
    assert run_export(j).exists()
    bad = job(tmp_path, frames=3)
    bad.out_path = tmp_path  # una carpeta no puede ser el archivo de salida
    with pytest.raises(RuntimeError):
        run_export(bad)


def test_gpu_export_is_deterministic_and_valid():
    # subproceso con la plataforma Qt real: los tests de UI fuerzan "offscreen", que no da OpenGL
    import os
    import sys
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run([sys.executable, "-I", str(Path(__file__).parent / "export_gpu_check.py")],
                       capture_output=True, text=True, env=env, timeout=180)
    if r.stdout.strip().startswith("SKIP"):
        pytest.skip(r.stdout.strip())
    assert r.returncode == 0 and r.stdout.strip() == "OK", r.stdout + r.stderr[-800:]


def wait_for(runner: ExportRunner, signal_names, timeout_ms=90_000):
    loop, got = QEventLoop(), {}
    for name in signal_names:
        getattr(runner, name).connect(lambda *a, n=name: (got.setdefault(n, a), loop.quit()) if n != "progress" else got.setdefault("progress", []).append(a))
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return got


def test_runner_exports_in_separate_process(qapp, tmp_path):
    runner = ExportRunner()
    runner.start(job(tmp_path, frames=24))
    got = wait_for(runner, ["progress", "finished", "failed"])
    assert "failed" not in got, got.get("failed")
    assert Path(got["finished"][0]).exists() and got["progress"][-1][0] == 24


def test_runner_cancel_cleans_up(qapp, tmp_path):
    runner = ExportRunner()
    j = job(tmp_path, frames=600)
    runner.start(j)
    threading.Timer(4.0, runner.cancel).start()  # da tiempo a que arranque el proceso
    got = wait_for(runner, ["cancelled", "finished", "failed"])
    assert "cancelled" in got and not j.out_path.exists() and not runner.running
