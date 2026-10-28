import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.engine.engine import Params
from marea.render.scene import Scene, Style

SIZE = (270, 480)
STYLE = Style(title="Marea de luz", artist="Demo")


def frame(t_ms, size=SIZE, **kw):
    scene = Scene(exact_lyrics(), demo_cover(), size, style=kw.pop("style", STYLE), **kw)
    return scene.snapshot(t_ms).toarray(colorType=skia.kRGBA_8888_ColorType)


def test_frame_is_deterministic_and_opaque():
    a, b = frame(12500), frame(12500)
    assert a.shape == (480, 270, 4) and np.array_equal(a, b)
    assert a[..., 3].min() == 255  # el fondo cubre todo el lienzo: sin bordes ni huecos


def test_background_has_no_visible_edges_over_time():
    # fondo solo: cualquier instante cubre el lienzo y no hay filas/columnas negras en los bordes
    scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE)
    for t in (0, 9.7, 31.2, 77.7):
        s = skia.Surface(*SIZE)
        scene.bg.draw(s.getCanvas(), t)
        px = s.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)
        assert px[..., 3].min() == 255
        assert px[..., :3].sum(axis=2).min() > 30  # el velo es 22 %: nada llega a negro


def test_active_line_is_brighter_than_inactive():
    img = frame(12500).astype(int).sum(axis=2)  # t = 12.5 s: "las luces se derriten en el mar" sonando
    bright = (img > 650).sum()  # blanco ≈ 765; el fondo azul y las líneas inactivas quedan muy por debajo
    assert bright > 200
    with_bloom = frame(12500).astype(int)
    no_bloom = frame(12500, style=Style(bloom=False)).astype(int)
    assert (with_bloom - no_bloom).sum() > 0  # el bloom solo suma luz


def credited_scene(credit=True):
    ly = exact_lyrics()
    ly.credit, ly.writers = "Apple Music", ["Ana Pérez", "Beto Ruiz"]
    return Scene(ly, demo_cover(), SIZE, style=Style(credit=credit, title="", artist="", wm_text=""))


def test_credit_is_drawn_after_the_last_line_and_can_be_turned_off():
    end = exact_lyrics().lines[-1].end + 800
    with_credit, without = credited_scene(True), credited_scene(False)
    assert [L.kind for L in with_credit.engine.items][-1] == "credit" and not any(L.is_credit for L in without.engine.items)
    a = np.asarray(with_credit.snapshot(end).toarray())[:, :, :3].astype(int)
    b = np.asarray(without.snapshot(end).toarray())[:, :, :3].astype(int)
    diff = np.abs(a - b).sum(axis=2)
    assert (diff > 30).sum() > 50  # el crédito deja píxeles claros donde sin él no hay nada
    rows = np.where((diff > 30).any(axis=1))[0]
    assert rows.min() > SIZE[1] * 0.3  # y está en la parte baja del escenario, no encima de la letra activa


def test_credit_wraps_long_names_and_never_overflows_the_stage():
    from marea.render.text import Fonts, layout_credit
    rows, csize, height = layout_credit([("Written by", ", ".join(f"Compositor Número {i}" for i in range(12))),
                                         ("Provided by", "Spicy Lyrics · uploaded by alguien, made by otra persona")],
                                        Fonts(), 70.0, 600.0)
    assert len(rows[0]) > 2 and height > 0  # la lista larga se parte en varias filas
    for block in rows:
        for row in block:
            word, x, bold = row[-1]
            assert x + Fonts().text(word, csize, bold)[1] <= 600.0 + 1e-6
    assert layout_credit([], Fonts(), 70.0, 600.0)[2] == 0.0


def test_vibrato_and_params_change_the_frame():
    assert not np.array_equal(frame(12500), frame(12500, params=Params(emph=False)))


def test_gpu_matches_cpu_and_is_deterministic():
    # en subproceso y con la plataforma Qt real: los tests de UI fuerzan "offscreen", que no da OpenGL
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run([sys.executable, "-I", str(Path(__file__).parent / "gpu_check.py")], capture_output=True,
                       text=True, env=env, timeout=120)
    out = r.stdout.strip()
    if out.startswith("SKIP"):
        pytest.skip(out)
    assert r.returncode == 0 and out.startswith("OK"), out + r.stderr[-500:]