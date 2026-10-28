"""Exporta la canción demo del prototipo (marea/demo_song.py) a TTML estilo Apple Music.

    python tools/export_demo_ttml.py   ->  assets/demo/marea-de-luz.ttml
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from marea.demo_song import SONG, words  # noqa: E402

OUT = ROOT / "assets" / "demo" / "marea-de-luz.ttml"


def clock(sec):
    ms = round(sec * 1000)
    return f"{ms // 60000:02d}:{ms % 60000 / 1000:06.3f}"


def spans(ws, wrap=False):
    out = []
    for wi, word in enumerate(ws):
        for si, (text, b, e) in enumerate(word):
            if wrap and wi == 0 and si == 0:
                text = "(" + text
            if wrap and wi == len(ws) - 1 and si == len(word) - 1:
                text += ")"
            out.append(f'<span begin="{clock(b)}" end="{clock(e)}">{text}</span>')
        if wi < len(ws) - 1:
            out.append(" ")
    return "".join(out), ws[-1][-1][2]


ps = []
for t, d, x, duet, bg in SONG:
    body, end = spans(words(x, t, d))
    if bg:
        inner, bend = spans(words(bg[2], bg[0], bg[1]), wrap=True)
        body += f'<span ttm:role="x-bg">{inner}</span>'
        end = max(end, bend)
    ps.append(f'      <p begin="{clock(t)}" end="{clock(end)}" ttm:agent="{"v2" if duet else "v1"}">{body}</p>')

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
    'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" itunes:timing="Word" xml:lang="es">\n'
    '  <head><metadata><ttm:agent type="person" xml:id="v1"/><ttm:agent type="person" xml:id="v2"/></metadata></head>\n'
    '  <body dur="00:54.000">\n    <div begin="00:06.000" end="00:52.400">\n' + "\n".join(ps) +
    "\n    </div>\n  </body>\n</tt>\n", encoding="utf-8")
print(OUT)
