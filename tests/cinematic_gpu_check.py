"""Chequeo GPU + Cinemático en proceso aparte (plataforma Qt real con OpenGL).

Imprime `OK <diferencia media GPU-CPU>` / `SKIP <motivo>`; sale con código 1 si falla.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import skia  # noqa: E402

from marea.demo_cover import demo_cover  # noqa: E402
from marea.demo_song import exact_lyrics  # noqa: E402
from marea.export import ffmpeg  # noqa: E402
from marea.model.cinematic import CinematicConfig  # noqa: E402
from marea.render.cinematic import probe_video  # noqa: E402
from marea.render.scene import Scene, Style  # noqa: E402

SIZE, STYLE = (270, 480), Style(title="Marea de luz", artist="Demo")

try:
    from marea.render.gpu import GpuContext
    g = GpuContext()
except RuntimeError as e:
    print("SKIP", e)
    sys.exit(0)

tmp = Path(tempfile.mkdtemp())
try:
    vid = tmp / "cine.mp4"
    frames_raw = np.zeros((30, 160, 120, 4), dtype=np.uint8)
    frames_raw[..., 0] = 220
    frames_raw[..., 1] = np.linspace(30, 200, 160, dtype=np.uint8)[None, :, None]
    frames_raw[..., 2] = np.linspace(40, 240, 120, dtype=np.uint8)[None, None, :]
    frames_raw[..., 3] = 255
    subprocess.run(
        [
            ffmpeg.find_ffmpeg("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "120x160", "-r", "30", "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
            str(vid),
        ],
        input=frames_raw.tobytes(),
        check=True,
    )
    res = probe_video(vid)
    cfg = CinematicConfig(enabled=True)
    cfg.set_primary_video(res)
    cfg.add_interval(11500.0, 14500.0, video_start_ms=0.0, video_end_ms=1000.0, fade_in_ms=400.0, fade_out_ms=400.0)

    surf = g.make_surface(*SIZE)
    gpu_frames = []
    for _ in range(2):
        scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE, gpu=True, cinematic=cfg, async_video=False)
        try:
            scene.engine.warmup(12500)
            scene.render(surf.getCanvas())
            gpu_frames.append(g.read_rgba(surf).astype(int))
        finally:
            scene.close()
    deterministic = np.array_equal(*gpu_frames)

    cpu_scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE, gpu=False, cinematic=cfg, async_video=False)
    try:
        cpu = cpu_scene.snapshot(12500).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
    finally:
        cpu_scene.close()

    diff = float(np.abs(gpu_frames[0] - cpu).mean())
    ok = deterministic and diff < 3.0
    print("OK" if ok else "FAIL", f"{diff:.3f}", "deterministic" if deterministic else "NOT-deterministic")
    sys.exit(0 if ok else 1)
finally:
    g.close()
    shutil.rmtree(tmp, ignore_errors=True)
