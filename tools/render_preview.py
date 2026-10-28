"""Frames estáticos de la demo para revisar el render:  python tools/render_preview.py  ->  out/preview_*.png"""
import sys
import time
from pathlib import Path

import skia

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from marea.demo_cover import demo_cover  # noqa: E402
from marea.demo_song import exact_lyrics  # noqa: E402
from marea.render.scene import Scene, Style  # noqa: E402

OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
SHOTS = [("a_bg_y_letras", 17000), ("b_dueto", 23000), ("c_interludio", 30500)]  # t en ms

if __name__ == "__main__":
    cover = demo_cover()
    style = Style(title="Marea de luz", artist="Demo · letra original")
    for name, t in SHOTS:
        scene = Scene(exact_lyrics(), cover, (1080, 1920), style=style)
        t0 = time.perf_counter()
        img = scene.snapshot(t)
        print(f"{name}: {(time.perf_counter() - t0) * 1000:.0f} ms (incluye 3 s de pre-roll)")
        img.save(str(OUT / f"preview_{name}.png"), skia.kPNG)
    # hoja de contacto a media escala para revisar de un vistazo
    sheet = skia.Surface(3 * 540 + 40, 960 + 20)
    c = sheet.getCanvas()
    c.clear(skia.ColorSetRGB(20, 20, 24))
    for i, (name, _) in enumerate(SHOTS):
        img = skia.Image.open(str(OUT / f"preview_{name}.png"))
        c.drawImageRect(img, skia.Rect.MakeXYWH(10 + i * 550, 10, 540, 960), skia.SamplingOptions(skia.FilterMode.kLinear))
    sheet.makeImageSnapshot().save(str(OUT / "preview_hoja.png"), skia.kPNG)
    print(OUT / "preview_hoja.png")
