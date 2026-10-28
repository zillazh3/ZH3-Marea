"""Instancias estáticas de Inter Tight (OFL) a partir de la variable: skia no aplica 'wght' en este backend.

    python tools/build_fonts.py      # requiere fonttools (solo para este script)
"""
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
NAMES = {400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold"}

for w, name in NAMES.items():
    f = instantiateVariableFont(TTFont(FONTS / "InterTight-Variable.ttf"), {"wght": w}, updateFontNames=True)
    f.save(FONTS / f"InterTight-{name}.ttf")
    print(f"InterTight-{name}.ttf")
