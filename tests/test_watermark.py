import numpy as np
import pytest
import skia
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QPushButton

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.render.scene import WM_FADE_MS, Scene, Style
from marea.ui.screens import SettingsBody
from marea.ui.services import Services

SIZE = (270, 480)


class FakeSecrets:
    def __init__(self):
        self.data = {}

    def get(self, name):
        return self.data.get(name, "")

    def set(self, name, value):
        self.data[name] = value.strip()

    def delete(self, name):
        self.data.pop(name, None)


def frame(scene, t_ms):
    scene.engine.warmup(t_ms)
    s = skia.Surface(*SIZE)
    scene.render(s.getCanvas())
    return s.makeImageSnapshot().toarray()


def scene(**style):
    return Scene(exact_lyrics(), demo_cover(), SIZE, style=Style(**style))


def box_pixels(img, sc):
    b = sc._watermark_box(0.022 * sc.h)
    return img[int(b.top()):int(b.bottom()) + 1, int(b.left()):int(b.right()) + 1].astype(int)


def test_watermark_draws_in_the_corner_and_empty_text_draws_nothing():
    on, off = scene(), scene(wm_text="")
    a, b = frame(on, 2000), frame(off, 2000)
    assert np.abs(box_pixels(a, on) - box_pixels(b, on)).max() > 20
    outside = np.abs(a.astype(int) - b.astype(int)).sum(axis=2) > 0
    ys, xs = np.nonzero(outside)  # lo único distinto entre ambas es la marca, y cabe en su caja con la sombra
    box = on._watermark_box(0.022 * on.h)
    assert ys.min() >= box.top() - 8 and ys.max() <= 0.85 * on.h and xs.max() <= box.right() + 8


def test_watermark_stays_out_of_tiktok_safe_zone_on_both_sides():
    for side in ("left", "right"):
        sc = scene(wm_side=side, wm_glyph=True)
        box = sc._watermark_box(0.022 * sc.h)
        assert box.bottom() <= 0.86 * sc.h  # fuera del 15 % inferior
        assert box.left() >= 0 and box.right() <= sc.w * (1 - 0.1)  # lejos de la columna de botones
    assert scene(wm_side="right")._watermark_box(10).left() > scene(wm_side="left")._watermark_box(10).left()


def test_watermark_fades_in_from_clip_start():
    sc = scene()
    sc.clip_start_ms = 2000
    start = box_pixels(frame(sc, 2000), sc)
    bare = box_pixels(frame(scene(wm_text=""), 2000), sc)
    assert np.abs(start - bare).max() <= 2  # recién empieza el clip: invisible
    sc2 = scene()
    sc2.clip_start_ms = 2000 - WM_FADE_MS
    assert np.abs(box_pixels(frame(sc2, 2000), sc2) - bare).max() > 20  # fundido completo


def test_watermark_yields_to_the_sung_line_when_they_overlap():
    sc = scene()
    sc.engine.warmup(8000)
    box = sc._watermark_box(0.022 * sc.h)
    assert sc._wm_dodge(box, 0.022 * sc.h) == 0.0  # el layout normal no estorba
    active = max((L for L in sc.engine.items if not L.is_gap), key=lambda L: L.act)
    active.y.p = box.top() - sc.stage.top() - 0.2 * active.h  # la línea cantada baja hasta la marca
    assert sc._wm_dodge(box, 0.022 * sc.h) > 0.5


def test_export_frames_are_still_deterministic_with_watermark():
    from marea.export.job import ExportJob, render_frames
    from marea.demo_cover import demo_cover as dc
    job = ExportJob(exact_lyrics(), bytes(dc(64).encodeToData()), None, 4000, 4300, size=SIZE, prefer_gpu=False,
                    style=Style(wm_glyph=True))
    a, b = [np.stack(list(render_frames(job))) for _ in range(2)]
    assert np.array_equal(a, b)


# ---- Ajustes: claves y exportación ----

class FakeSpicy:
    """Spicy Lyrics sin red: los tests de Ajustes no deben salir a internet con una llave de mentira."""

    def __init__(self):
        self.checked, self.error = 0, None

    def check_key(self):
        self.checked += 1
        if self.error:
            raise self.error

    def fetch(self, q=None):
        return None


@pytest.fixture
def settings_win(make_window, qapp, tmp_path):
    secrets = FakeSecrets()
    services = Services(QSettings(str(tmp_path / "k.ini"), QSettings.IniFormat), secrets=secrets, spicy=FakeSpicy())
    win = make_window(services=services)
    win.show()
    return win, win.pages.widget(5).findChild(SettingsBody), secrets


def test_api_key_is_saved_to_secrets_never_to_settings_and_can_be_cleared(settings_win):
    win, body, secrets = settings_win
    edit = body.key_edits["spicy_lyrics"]
    save = next(b for b in body.findChildren(QPushButton) if b.text() == "Guardar")
    save.click()  # vacío
    assert not secrets.data and win.toasts.count() == 1
    edit.setText("  clave-secreta  ")
    save.click()
    assert secrets.data == {"spicy_lyrics": "clave-secreta"} and edit.text() == "" and edit.placeholderText() == "Guardada"
    assert "clave-secreta" not in "".join(str(body.settings.value(k)) for k in body.settings.allKeys())
    next(b for b in body.findChildren(QPushButton) if b.text() == "Borrar").click()
    assert not secrets.data


def test_saving_the_spicy_key_checks_it_and_reports_the_result(settings_win):
    from marea.providers.lyrics.spicy import SpicyKeyError
    from tests.test_spotify_ui import until
    win, body, secrets = settings_win
    spicy, edit = body.services.spicy, body.key_edits["spicy_lyrics"]
    save = next(b for b in body.findChildren(QPushButton) if b.text() == "Guardar")
    edit.setText("sl_pk_buena")
    save.click()
    assert until(lambda: spicy.checked == 1 and win.toasts.count() == 2)  # «guardada» + «aceptó la llave»
    spicy.error = SpicyKeyError("Spicy Lyrics rechazó tu llave.")
    edit.setText("sl_pk_mala")
    save.click()
    assert until(lambda: spicy.checked == 2 and win.toasts.count() == 4)
    assert secrets.data == {"spicy_lyrics": "sl_pk_mala"}  # la llave se guarda aunque falle la comprobación (puede ser un fallo de red)


def test_export_settings_feed_the_editor_menu(settings_win, tmp_path):
    win, body, _ = settings_win
    body.preset.setCurrentIndex(body.preset.findData("square"))
    body.encoder.setCurrentIndex(body.encoder.findData("nvenc"))
    win.load_demo()
    win.editor.menu.aboutToShow.emit()
    assert win.editor.preset_actions["square"].isChecked()
    assert "Cuadrado" in win.editor.summary.text()
    body.settings.setValue("output_dir", str(tmp_path))
    assert win.editor._output_dir() == tmp_path


def test_watermark_tab_persists_and_reaches_the_export_job(settings_win):
    win, _, _ = settings_win
    win.load_demo()
    e = win.editor
    e.ed_wm.setText("Mi Canal")
    e.cmb_wm_side.setCurrentIndex(1)
    e.cb_wm_glyph.setChecked(True)
    job = e.build_job()
    assert (job.style.wm_text, job.style.wm_side, job.style.wm_glyph) == ("Mi Canal", "right", True)
    assert e.settings.value("watermark/text") == "Mi Canal" and e.settings.value("watermark/side") == "right"
