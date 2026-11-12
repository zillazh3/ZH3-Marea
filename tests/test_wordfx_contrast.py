"""Color de las palabras según el fondo: función pura (contraste, tono) y de punta a punta en la escena."""
import colorsys

import numpy as np
import pytest
import skia

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import library
from marea.wordfx.contrast import TARGET, adapt, contrast
from marea.wordfx.plan import build_plan


def hue(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[0] * 360


def hue_gap(a, b):
    d = abs(hue(a) - hue(b))
    return min(d, 360 - d)


ORANGE, PASTEL = (255, 147, 63), (191, 180, 255)


def test_light_color_is_kept_on_a_dark_background_and_darkened_on_a_light_one():
    assert adapt(ORANGE, (20, 20, 40)) == ORANGE  # ya contrasta: no se toca
    for bg in ((255, 255, 255), (250, 230, 120), (200, 220, 250)):
        for c in (ORANGE, PASTEL):
            out = adapt(c, bg)
            assert contrast(out, bg) >= TARGET - 0.05 and out != c
            assert sum(out) < sum(c) and hue_gap(out, c) < 12  # más oscuro, mismo tono


def test_dark_colors_get_lighter_on_a_dark_background_keeping_the_hue():
    out = adapt((60, 70, 120), (15, 15, 30))
    assert contrast(out, (15, 15, 30)) >= TARGET - 0.05 and sum(out) > 250 and hue_gap(out, (60, 70, 120)) < 12


def test_adaptation_is_continuous_in_the_background():
    """Ni al cruzar de «aclarar» a «oscurecer» ni al empezar/terminar el efecto hay saltos bruscos."""
    for text in (ORANGE, PASTEL, (250, 250, 250), (60, 70, 120)):
        prev = None
        for v in range(0, 256):
            out = adapt(text, (v, v, v))
            if prev is not None:
                assert max(abs(a - b) for a, b in zip(out, prev)) <= 12, (text, v)  # por cada nivel de gris del fondo
            prev = out


def solid(rgb) -> skia.Image:
    s = skia.Surface(64, 64)
    s.getCanvas().drawColor(skia.ColorSetRGB(*rgb))
    return s.makeImageSnapshot()


def lyrics():
    syls = [Syllable(w, 1000 + i * 500, 1000 + (i + 1) * 500, word_end=i < 2) for i, w in enumerate("mi fuego eterno".split())]
    syls[-1].end += 1500
    return Lyrics([Line(syls[0].begin, syls[-1].end, syllables=syls)])


def frame(cover_rgb):
    ly = lyrics()
    plan = build_plan(ly, library.load(library.BUNDLED), track_id="t", overrides=[])
    return Scene(ly, solid(cover_rgb), (270, 480), style=Style(), wordfx=plan).snapshot(2600).toarray(colorType=skia.kRGBA_8888_ColorType)


def test_word_darkens_over_a_light_cover_and_renders_identically_twice():
    light, dark = frame((245, 235, 200)), frame((20, 24, 60))
    assert np.array_equal(light, frame((245, 235, 200)))  # determinista
    box = (slice(180, 300), slice(20, 250))  # franja donde cae el verso
    lum = lambda a, bg: float(np.abs(a[box][..., :3].astype(int) - bg).sum(axis=-1).max())  # cuánto se separa el trazo del fondo
    assert lum(light, light[5, 5, :3].astype(int)) > 150  # sobre fondo claro la palabra sigue destacando (trazo oscuro)
    assert lum(dark, dark[5, 5, :3].astype(int)) > 150


class WhiteCinematic:
    """Cinemático de prueba: tapa el fondo con blanco, dibujando en píxeles del lienzo real."""
    def __init__(self, w, h):
        self.w, self.h = w, h

    def active_at(self, t_ms):
        return True

    def draw(self, canvas, base, t, lyric_act=0.0):
        canvas.drawRect(skia.Rect.MakeWH(self.w, self.h), skia.Paint(Color=skia.ColorWHITE))

    def close(self):
        pass


def test_backdrop_probe_sees_cinematic_images_not_only_the_cover():
    ly = lyrics()
    plan = build_plan(ly, library.load(library.BUNDLED), track_id="t", overrides=[])
    s = Scene(ly, solid((20, 24, 60)), (270, 480), style=Style(), wordfx=plan)
    s.engine.warmup(2600)
    dark = s._backdrop(20, 200, 200, 40)
    s.cinematic_bg = WhiteCinematic(270, 480)
    s._probe[2][0] = None  # invalida la caché por fotograma
    assert max(dark) < 80 and min(s._backdrop(20, 200, 200, 40)) > 230


def test_scene_ignores_the_background_when_the_option_is_off():
    from dataclasses import replace
    ly = lyrics()
    plan = build_plan(ly, library.load(library.BUNDLED), track_id="t", overrides=[])
    light = (245, 235, 200)

    def snap(p):
        return Scene(ly, solid(light), (270, 480), style=Style(), wordfx=p).snapshot(2600).toarray(colorType=skia.kRGBA_8888_ColorType)

    on, off = snap(plan), snap(replace(plan, adapt_bg=False))
    assert plan.adapt_bg and not np.array_equal(on, off)  # con la opción, el color cambia sobre un fondo claro
    assert np.array_equal(off, snap(replace(plan, adapt_bg=False)))
