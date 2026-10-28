"""Chequeo GPU en proceso aparte (con la plataforma Qt real; la offscreen de los tests no da OpenGL).

Imprime `OK <diferencia media GPU-CPU>` / `SKIP <motivo>`; sale con código 1 si algo no coincide.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import skia  # noqa: E402

from marea.demo_cover import demo_cover  # noqa: E402
from marea.demo_song import exact_lyrics  # noqa: E402
from marea.render.scene import Scene, Style  # noqa: E402

SIZE, STYLE = (270, 480), Style(title="Marea de luz", artist="Demo")

try:
    from marea.render.gpu import GpuContext
    g = GpuContext()
except RuntimeError as e:
    print("SKIP", e)
    sys.exit(0)

try:
    surf = g.make_surface(*SIZE)
    frames = []
    for _ in range(2):  # dos escenas independientes sobre la misma superficie
        scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE, gpu=True)
        scene.engine.warmup(12500)
        scene.render(surf.getCanvas())
        frames.append(g.read_rgba(surf).astype(int))
    deterministic = np.array_equal(*frames)
finally:
    g.close()

cpu_scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE)
cpu = cpu_scene.snapshot(12500).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
diff = float(np.abs(frames[0] - cpu).mean())
print("OK" if deterministic and diff < 3.0 else "FAIL", f"{diff:.3f}", "deterministic" if deterministic else "NOT-deterministic")
sys.exit(0 if deterministic and diff < 3.0 else 1)
