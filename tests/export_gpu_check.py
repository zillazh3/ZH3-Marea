"""Export por GPU en proceso aparte (plataforma Qt real): determinismo de frames y mp4 válido.

Imprime `OK` / `SKIP <motivo>`; sale con 1 si algo falla.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import skia  # noqa: E402

from marea.demo_cover import demo_cover  # noqa: E402
from marea.demo_song import exact_lyrics  # noqa: E402
from marea.export import ffmpeg  # noqa: E402
from marea.export.job import ExportJob, render_frames, run_export  # noqa: E402
from marea.render.gpu import make_context  # noqa: E402
from marea.render.scene import Style  # noqa: E402

ctx = make_context()
if not ctx.gpu:
    print("SKIP sin OpenGL")
    sys.exit(0)
ctx.close()

out = Path(tempfile.mkdtemp()) / "gpu.mp4"
job = ExportJob(exact_lyrics(), bytes(demo_cover().encodeToData()), out, 12500, 12500 + 15 / 30 * 1000,
                size=(270, 480), fps=30, style=Style(title="Marea de luz", artist="Demo"))
runs = [[hashlib.sha256(f.tobytes()).hexdigest() for f in render_frames(job)] for _ in range(2)]
deterministic = runs[0] == runs[1] and len(set(runs[0])) > 5
run_export(job)
info = json.loads(subprocess.run([ffmpeg.find_ffmpeg("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-of",
                                  "json", str(out)], capture_output=True, text=True, check=True).stdout)
v = info["streams"][0]
ok = deterministic and (v["codec_name"], v["width"], v["height"], v["nb_read_frames"]) == ("h264", 270, 480, "15")
print("OK" if ok else f"FAIL deterministic={deterministic} stream={v}")
sys.exit(0 if ok else 1)
