"""Colores dominantes de la portada: el efecto de cada palabra se mezcla con ellos para encajar con el fondo."""
import numpy as np
import skia

from marea.wordfx.fxtypes import RGB

SAMPLE = 32  # la portada se reduce a SAMPLE×SAMPLE antes de contar


def dominant_colors(cover: skia.Image, k: int = 3) -> tuple[RGB, ...]:
    """Hasta `k` colores representativos: cubetas de 4 bits por canal, ordenadas por área × (0.3 + saturación)."""
    surf = skia.Surface(SAMPLE, SAMPLE)
    surf.getCanvas().drawImageRect(cover, skia.Rect.MakeWH(SAMPLE, SAMPLE), skia.SamplingOptions(skia.FilterMode.kLinear))
    px = surf.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)[..., :3].reshape(-1, 3).astype(np.int32)
    keys = (px[:, 0] >> 4) * 256 + (px[:, 1] >> 4) * 16 + (px[:, 2] >> 4)
    sat = (px.max(axis=1) - px.min(axis=1)) / 255.0
    # shortcut: no se fusionan cubetas vecinas; una portada con degradado suave puede repetir tonos parecidos
    ranked = sorted(((float((keys == u).sum() * (0.3 + sat[keys == u].mean())), int(u)) for u in np.unique(keys)), reverse=True)
    return tuple(tuple(int(c) for c in px[keys == u].mean(axis=0)) for _, u in ranked[:k])
