"""Logo de ZH3 Marea: genera los SVG de las propuestas y el PNG comparativo (skia, sin cairosvg).

    python tools/build_icons.py --compare
"""
import argparse
import struct
from pathlib import Path

import skia

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "logo" / "src"
PROPOSALS = ROOT / "assets" / "logo" / "proposals"
FONT = ROOT / "assets" / "fonts" / "InterTight-Variable.ttf"

SQUIRCLE = ("M136 0H376C460 0 512 52 512 136V376C512 460 460 512 376 512H136"
            "C52 512 0 460 0 376V136C0 52 52 0 136 0Z")
SOLID = "#3D46FF"  # mezcla de #2B56FF y #6A35FF para 16-32 px (sin degradado)


def wave(x0, x1, y, a, h, up=True):
    """Seno hecho de cúbicas con tangentes horizontales en los extremos."""
    n = max(1, round((x1 - x0) / h))
    step = (x1 - x0) / n
    pts = [(x0 + i * step, y + (-a if (i % 2 == 0) == up else a)) for i in range(n + 1)]
    d = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        d += f"C{xa + .45 * step:.1f} {ya:.1f} {xb - .45 * step:.1f} {yb:.1f} {xb:.1f} {yb:.1f}"
    return d


def concept(name, small):
    """-> (ondas [(d, ancho, opacidad)], puntos [(cx, cy, r)]). small: rejilla de 16 px (32 u = 1 px)."""
    w_side, w_mid = (32, 64) if small else (30, 46)
    y_top, y_mid, y_bot = (144, 256, 368) if small else (160, 256, 352)
    a_side, a_mid = (10, 22) if small else (12, 26)
    if name == "a":  # líneas de letra: largos escalonados, alineadas a la izquierda
        w = [(wave(96, 296, y_top, a_side, 50), w_side, .55),
             (wave(96, 416, y_mid, a_mid, 64), w_mid, 1),
             (wave(96, 344, y_bot, a_side, 50), w_side, .55)]
        return w, []
    if name == "b":  # marea: tres senos desfasados + punto del interludio
        w = [(wave(88, 424, y_top, a_side * 1.4, 56, up=False), w_side, .55),
             (wave(88, 376, y_mid, a_mid * 1.2, 72), w_mid, 1),
             (wave(88, 424, y_bot, a_side * 1.4, 56), w_side, .55)]
        return w, [(430, y_mid, 32 if small else 24)]
    # c: arriba/abajo casi planas (texto), la central es la ola
    w = [(wave(96, 352, y_top, 4, 128), w_side, .55),
         (wave(96, 384, y_mid, a_mid * 1.7, 64), w_mid, 1),
         (wave(96, 288, y_bot, 4, 96), w_side, .55)]
    return w, [(432, y_mid, 32 if small else 24)]


def svg(name, small=False, mono=False):
    waves, dots = concept(name, small)
    fill = "#fff" if mono else "url(#g)"
    body = "".join(
        f'<path d="{d}" fill="none" stroke="#fff" stroke-width="{w}" stroke-linecap="round" '
        f'stroke-linejoin="round" opacity="{o}"/>' for d, w, o in waves)
    body += "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff"/>' for x, y, r in dots)
    if mono:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{body}</svg>'
    bg = (f'<path d="{SQUIRCLE}" fill="{SOLID}"/>' if small else
          '<defs><linearGradient id="g" x1="0" y1="0" x2="512" y2="512" gradientUnits="userSpaceOnUse">'
          '<stop offset="0" stop-color="#2B56FF"/><stop offset="1" stop-color="#6A35FF"/></linearGradient></defs>'
          f'<path d="{SQUIRCLE}" fill="{fill}"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{bg}{body}</svg>'


def write_svgs():
    SRC.mkdir(parents=True, exist_ok=True)
    for n in "abc":
        (SRC / f"{n}.svg").write_text(svg(n), encoding="utf-8")
        (SRC / f"{n}-16.svg").write_text(svg(n, small=True), encoding="utf-8")
        (SRC / f"{n}-mono.svg").write_text(svg(n, mono=True), encoding="utf-8")


def draw_svg(canvas, text, x, y, size):
    dom = skia.SVGDOM.MakeFromStream(skia.MemoryStream(text.encode()))
    dom.setContainerSize(skia.Size(size, size))
    canvas.save()
    canvas.translate(x, y)
    dom.render(canvas)
    canvas.restore()


def raster(text, size):
    s = skia.Surface(size, size)
    draw_svg(s.getCanvas(), text, 0, 0, size)
    return s.makeImageSnapshot()


def compare():
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    tf = skia.FontMgr().makeFromFile(str(FONT))
    font = lambda sz: skia.Font(tf, sz)
    ink, white = skia.Paint(Color=skia.Color(0xFF, 0xFF, 0xFF), AntiAlias=True), skia.Paint(AntiAlias=True)
    colw, W, H = 620, 620 * 3 + 40, 1230
    s = skia.Surface(W, H)
    c = s.getCanvas()
    c.clear(skia.Color(0x0E, 0x0D, 0x1A))
    ink.setAlphaf(.9)
    c.drawString("ZH3 Marea · propuestas de ícono", 40, 52, font(28), ink)
    names = {"a": "A · Líneas de letra", "b": "B · Marea", "c": "C · Letra que se vuelve ola"}
    for i, n in enumerate("abc"):
        x0 = 20 + i * colw
        ink.setAlphaf(.9)
        c.drawString(names[n], x0 + 20, 100, font(22), ink)
        draw_svg(c, svg(n), x0 + 20, 120, 400)
        # tamaños 128/64 (gradiente), 32/16 (sólido, rejilla de píxeles)
        draw_svg(c, svg(n), x0 + 440, 120, 128)
        draw_svg(c, svg(n), x0 + 440, 270, 64)
        y = 560
        for px, label in ((32, "32"), (16, "16")):
            c.drawImage(raster(svg(n, small=True), px), x0 + 20 + (0 if px == 32 else 80), y + (0 if px == 32 else 16))
        # 16 y 32 ampliados ×6 sin suavizar para ver nitidez
        for k, px in enumerate((32, 16)):
            img = raster(svg(n, small=True), px)
            c.drawImageRect(img, skia.Rect.MakeXYWH(x0 + 170 + k * 210, y - 8, px * 6, px * 6),
                            skia.SamplingOptions(skia.FilterMode.kNearest))
        ink.setAlphaf(.5)
        c.drawString("32 / 16 px reales  ·  ampliados ×6", x0 + 20, y + 218, font(14), ink)
        # monocromo sobre oscuro y sobre fondo tipo portada
        y = 820
        draw_svg(c, svg(n, mono=True), x0 + 20, y, 160)
        shader = skia.GradientShader.MakeLinear(
            [(0, 0), (260, 160)], [skia.Color(0xD8, 0x74, 0x1E), skia.Color(0x14, 0xB8, 0xA6)])
        c.save(); c.translate(x0 + 220, y)
        c.drawRoundRect(skia.Rect.MakeWH(260, 160), 16, 16, skia.Paint(Shader=shader))
        c.drawRoundRect(skia.Rect.MakeWH(260, 160), 16, 16, skia.Paint(Color=skia.Color(0, 0, 0, 70)))
        c.restore()
        draw_svg(c, svg(n, mono=True), x0 + 270, y + 10, 140)
        ink.setAlphaf(.5)
        c.drawString("monocromo blanco", x0 + 20, y + 185, font(14), ink)
        # maqueta de barra de título
        y = 1060
        c.drawRoundRect(skia.Rect.MakeXYWH(x0 + 20, y, 560, 40), 8, 8, skia.Paint(Color=skia.Color(0x1A, 0x19, 0x2B)))
        c.drawImage(raster(svg(n, small=True), 16), x0 + 34, y + 12)
        ink.setAlphaf(.8)
        c.drawString("ZH3 Marea — Camino solo por la ciudad", x0 + 62, y + 26, font(13), ink)
        # ícono 48 para barra de tareas / .ico
        draw_svg(c, svg(n), x0 + 20, y + 70, 48)
        draw_svg(c, svg(n), x0 + 90, y + 70, 24)
        ink.setAlphaf(.5)
        c.drawString("48 / 24", x0 + 130, y + 100, font(14), ink)
    out = PROPOSALS / "comparativa.png"
    s.makeImageSnapshot().save(str(out), skia.kPNG)
    print(out)


LOGO = ROOT / "assets" / "logo"
CHOSEN = "b"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def typeface(weight):
    name = {400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold"}[weight]
    return skia.FontMgr().makeFromFile(str(FONT.with_name(f"InterTight-{name}.ttf")))


def text_paths(text, weight, size, x, baseline, opacity=1):
    """Texto como <path> vectoriales (no depende de tener Inter Tight instalada). -> (svg, ancho)."""
    font = skia.Font(typeface(weight), size)
    glyphs = font.textToGlyphs(text)
    out, pen = [], x
    for g, adv in zip(glyphs, font.getWidths(glyphs)):
        d, glyph = "", skia.Path(font.getPath(g))
        glyph.transform(skia.Matrix.Translate(pen, baseline))
        for verb, pts in glyph:
            p = [f"{q.x():.1f} {q.y():.1f}" for q in pts]
            d += {skia.Path.kMove_Verb: lambda: "M" + p[0], skia.Path.kLine_Verb: lambda: "L" + p[1],
                  skia.Path.kQuad_Verb: lambda: "Q" + ",".join(p[1:]), skia.Path.kCubic_Verb: lambda: "C" + ",".join(p[1:]),
                  skia.Path.kClose_Verb: lambda: "Z"}[verb]()
        out.append(d)
        pen += adv
    return f'<path d="{"".join(out)}" fill="#fff" opacity="{opacity}"/>', pen - x


def wordmark():
    inner = svg(CHOSEN).split(">", 1)[1].rsplit("</svg>", 1)[0]  # defs + squircle + ondas
    size, base = 210, 256 + 0.36 * 210  # cap-height de Inter Tight ≈ 0.73 em, centrado en y=256
    zh3, w1 = text_paths("ZH3", 500, size, 568, base, .6)
    marea, w2 = text_paths(" Marea", 700, size, 568 + w1, base)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {568 + w1 + w2 + 56:.0f} 512">'
            f'{inner}{zh3}{marea}</svg>')


def ico(path):
    imgs = [bytes(raster(svg(CHOSEN, small=s <= 32), s).encodeToData()) for s in ICO_SIZES]
    off = 6 + 16 * len(imgs)
    head, body = struct.pack("<HHH", 0, 1, len(imgs)), b""
    for s, b in zip(ICO_SIZES, imgs):
        head += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(b), off + len(body))
        body += b
    path.write_bytes(head + body)


def build():
    LOGO.mkdir(parents=True, exist_ok=True)
    files = {"marea.svg": svg(CHOSEN), "marea-16.svg": svg(CHOSEN, small=True),
             "marea-mono.svg": svg(CHOSEN, mono=True), "marea-wordmark.svg": wordmark()}
    for name, text in files.items():
        (LOGO / name).write_text(text, encoding="utf-8")
    for s in (512, 1024):
        raster(files["marea.svg"], s).save(str(LOGO / f"marea-{s}.png"), skia.kPNG)
    raster(files["marea-16.svg"], 32).save(str(LOGO / "favicon.png"), skia.kPNG)
    ico(LOGO / "marea.ico")
    for p in sorted(LOGO.glob("marea*")) + [LOGO / "favicon.png"]:
        print(p.relative_to(ROOT), p.stat().st_size)
    # vista del wordmark para revisar (a 1/2 de tamaño, sobre fondo oscuro)
    wm = files["marea-wordmark.svg"]
    w = int(float(wm.split('viewBox="0 0 ')[1].split()[0]))
    s = skia.Surface(w // 2 + 40, 296)
    s.getCanvas().clear(skia.Color(0x0E, 0x0D, 0x1A))
    dom = skia.SVGDOM.MakeFromStream(skia.MemoryStream(wm.encode()))
    dom.setContainerSize(skia.Size(w // 2, 256))
    s.getCanvas().translate(20, 20)
    dom.render(s.getCanvas())
    s.makeImageSnapshot().save(str(PROPOSALS / "wordmark-preview.png"), skia.kPNG)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--build", action="store_true", help="variantes finales de la propuesta elegida")
    a = ap.parse_args()
    if a.compare:
        write_svgs()
        compare()
    if a.build:
        build()
