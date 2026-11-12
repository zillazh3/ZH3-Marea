"""Editor + Ajustes de Semantic Word FX: función apagada por defecto, plan en preview y export, y hot reload de los JSON."""
import json

import pytest

from marea.wordfx import library, rules, songs


@pytest.fixture
def win(make_window, qapp, tmp_path, monkeypatch):
    # los JSON del usuario viven en tmp_path: la prueba edita archivos sin tocar los reales
    monkeypatch.setattr(library, "user_path", lambda: tmp_path / "library.json")
    monkeypatch.setattr(rules, "user_path", lambda: tmp_path / "rules.json")
    monkeypatch.setattr(songs, "path_for", lambda tid: tmp_path / f"{tid}.json")
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w


def fx_words(editor):
    scene = editor.preview.scene
    return [w for L in scene._by_line.values() for w in L.words if w.fx is not None]


def test_off_by_default_and_nothing_imported_into_the_scene(win):
    e = win.editor
    assert e._fx_plan is None and fx_words(e) == [] and not e.cb_fxdebug.isVisibleTo(e) and not e._fx_watch.isActive()


def test_enabling_the_flag_builds_the_plan_and_export_gets_the_same_one(win):
    e = win.editor
    e.settings.setValue("experimental/wordfx", True)
    e._wordfx_sync()
    assert e._fx_plan is not None and fx_words(e) and e._fx_watch.isActive()
    assert e.build_job().wordfx is e._fx_plan  # preview y export usan el mismo plan
    e.settings.setValue("experimental/wordfx", False)
    e._wordfx_sync()
    assert e._fx_plan is None and fx_words(e) == [] and e.build_job().wordfx is None and not e._fx_watch.isActive()


def test_intensity_slider_rebuilds_with_the_new_intensity(win):
    e = win.editor
    e.settings.setValue("experimental/wordfx", True)
    e._wordfx_sync()
    e.sl_fx.setValue(0)
    e._wordfx_sync()  # el temporizador de 250 ms lo haría solo; aquí se fuerza
    assert e._fx_plan.intensity == 0 and all(w.params.erode == 0 for w in e._fx_plan.words.values())


def test_hot_reload_picks_up_an_edited_library_and_survives_a_broken_one(win, tmp_path):
    e = win.editor
    e.settings.setValue("experimental/wordfx", True)
    e._wordfx_sync()
    base = set(e._fx_plan.words)
    assert base
    user = tmp_path / "library.json"
    user.write_text(json.dumps({"x": {"lemmas": {"es": ["ciudad"]}, "vector": dict.fromkeys(library.AXES, 0.0),
                                      "primitives": ["glow"]}}), encoding="utf-8")
    e._wordfx_sync()  # lo que haría el temporizador al ver otra fecha
    assert set(e._fx_plan.words) != base and all(w.match.categories == ("x",) for w in e._fx_plan.words.values())
    good, msgs = e._fx_plan, []
    e.notify = lambda text, kind="info": msgs.append((kind, text))
    user.write_text("{roto", encoding="utf-8")
    e._wordfx_sync()
    assert e._fx_plan is good and msgs and msgs[0][0] == "error" and "library.json" in msgs[0][1]


def test_settings_page_has_the_experimental_checkbox_off_by_default(win):
    boxes = [w for w in win.findChildren(type(win.editor.cb_fxdebug)) if w.text() == "Efectos semánticos por palabra"]
    assert len(boxes) == 1 and not boxes[0].isChecked()


def test_background_color_toggle_rebuilds_the_plan_and_is_remembered(win):
    e = win.editor
    e.settings.setValue("experimental/wordfx", True)
    e._wordfx_sync()
    assert e.cb_fxbg.isVisibleTo(e) and e.cb_fxbg.isChecked() and e._fx_plan.adapt_bg  # activado por defecto
    e.cb_fxbg.setChecked(False)
    assert not e._fx_plan.adapt_bg and e.settings.value("wordfx/adapt_bg", True, type=bool) is False
    e.cb_fxbg.setChecked(True)
    assert e._fx_plan.adapt_bg
