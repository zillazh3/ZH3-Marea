"""Corrientes y Anzuelo en el Editor: aplicar, guardar, recordar en el proyecto y encolar variantes."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog

from marea.hooks.analyze import load_weights, suggest
from marea.project import load_project_file, save_project


@pytest.fixture
def win(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w


@pytest.fixture
def captured(win, monkeypatch):
    items = []
    monkeypatch.setattr(win.queue, "add", items.append)
    return items


def pick(editor, name):
    pc = editor._presets
    pc._select(name)
    assert pc.selected_name() == name
    return pc


def test_builtin_presets_are_listed_with_read_only_actions(win):
    pc = win.editor._presets
    assert [pc.list.item(i).data(Qt.UserRole) for i in range(pc.list.count())][:4] == ["Bajamar", "Pleamar", "Niebla", "Abisal"]
    pick(win.editor, "Abisal")
    assert pc.btn_apply.isEnabled() and not pc.btn_update.isEnabled() and not pc.act_del.isEnabled() and not pc.act_rename.isEnabled()


def test_apply_changes_the_look_and_marks_modified_afterwards(win):
    e = win.editor
    pc = pick(e, "Abisal")
    pc.apply_selected()
    assert e.style.glow_rgb == (0x5B, 0x6C, 0xFF) and e.style.bg_veil == pytest.approx(0.5)
    assert e.preview.scene.style.glow_rgb == (0x5B, 0x6C, 0xFF)  # la preview se reconstruyó con el look nuevo
    assert e.sl_fx.value() == 110 and pc.applied == "Abisal" and pc.lb_state.text().endswith("aplicada")
    e.style.glow_gain = 2.0  # un cambio a mano: el indicador lo nota
    pc.update_modified()
    assert "modificado" in pc.lb_state.text() and "color" in pc.lb_state.text()


def test_partial_apply_only_touches_checked_sections(win):
    e = win.editor
    pc = pick(e, "Abisal")
    for sec, cb in pc.cb_sections.items():
        cb.setChecked(sec == "fondo")
    assert pc.btn_apply.text() == "Aplicar (1/5)"  # el estado parcial queda visible aunque las casillas vivan en el menú ⋯
    before = e.style.glow_rgb
    pc.apply_selected()
    assert e.style.bg_veil == pytest.approx(0.5) and e.style.glow_rgb == before and e.sl_fx.value() == 100


def test_bajamar_restores_the_default_look(win):
    e = win.editor
    pick(e, "Abisal").apply_selected()
    pc = pick(e, "Bajamar")
    pc.apply_selected()
    assert e.style.glow_rgb is None and e.style.bg_veil == pytest.approx(0.22) and e.sl_fx.value() == 100


def test_save_as_update_rename_duplicate_delete(win, monkeypatch):
    e = win.editor
    pc = pick(e, "Pleamar")
    pc.apply_selected()
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Mi corriente", True)))
    pc.save_as()
    assert pc.selected_name() == "Mi corriente" and pc.applied == "Mi corriente"
    assert pc.selected().preset["color"]["glow_gain"] == pytest.approx(1.6)
    e.style.glow_gain = 0.5
    pc.update_selected()
    assert pc.selected().preset["color"]["glow_gain"] == pytest.approx(0.5)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Renombrada", True)))
    pc.rename_selected()
    assert pc.selected_name() == "Renombrada"
    pc.duplicate_selected()
    assert pc.selected_name() == "Renombrada (copia)"
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    pc.delete_selected()
    assert "Renombrada (copia)" not in pc._infos and "Renombrada" in pc._infos


def test_project_remembers_applied_preset_and_look(win, make_window, qapp, tmp_path):
    e = win.editor
    pick(e, "Niebla").apply_selected()
    path = save_project(e.project, tmp_path / "p.marea.json", editor_state=e.editor_state())
    w2 = make_window("b.ini")
    w2.show()
    proj, state = load_project_file(path)
    w2.editor.set_project(proj, editor_state=state)
    qapp.processEvents()
    e2 = w2.editor
    assert e2._presets.applied == "Niebla" and e2.params.motion == "suave"
    assert e2.style.bg_blur == pytest.approx(1.8) and e2.style.text_rgb == (0xF4, 0xF6, 0xFA) and e2.sl_fx.value() == 70
    assert e2._presets.lb_state.text().endswith("aplicada")


def test_old_project_state_without_new_fields_still_restores(win, make_window, qapp, tmp_path):
    e = win.editor
    state = e.editor_state()
    for k in ("fx", "preset"):
        state.pop(k)
    state["style"] = {"bloom": False, "font_family": "", "wm_text": "viejo"}  # como lo guardaba la v0.1
    state["params"] = {"motion": "onda"}
    e.set_project(e.project, editor_state=state)
    assert e.style.bloom is False and e.params.motion == "onda"
    assert e.style.wm_text != "viejo"  # sin los campos nuevos, la marca de agua sigue viniendo de los ajustes globales
    assert e.style.glow_gain == 1.0


# ---------------- Anzuelo ----------------

def run_suggestions(editor, targets=(15, 30)):
    editor._hooks.results = suggest(editor.project.lyrics, editor.project.title, targets, weights=load_weights())
    editor._hooks.chip_target = float(targets[-1])
    editor._hooks.show_results()


def test_suggestion_cards_apply_range_and_highlight(win, qapp):
    e = win.editor
    run_suggestions(e)
    hk = e._hooks
    assert hk.list.count() > 0
    s = hk.list.item(0).data(Qt.UserRole)
    hk._hover(s)
    assert e.hook_hover == (s.first, s.last)
    hk._hover(None)
    assert e.hook_hover is None
    hk.apply(s)
    assert (e.first, e.last) == (s.first, s.last)


def test_enqueue_selected_uses_the_suggested_range_with_preroll(win, captured):
    e = win.editor
    run_suggestions(e)
    hk = e._hooks
    hk.list.selectAll()
    n = hk.list.count()
    hk.enqueue_selected()
    assert len(captured) == n
    for item, s in zip(captured, [hk.list.item(i).data(Qt.UserRole) for i in range(n)]):
        assert (item.job.start_ms, item.job.end_ms) == (s.start_ms, s.end_ms) and item.audio is None
        assert f"versos {s.first + 1}–{s.last + 1}" in item.job.out_path.name


def test_enqueue_variants_makes_one_job_per_preset_with_its_name_in_the_file(win, captured, monkeypatch):
    e = win.editor
    run_suggestions(e)
    hk = e._hooks
    infos = [i for i in hk.store.list() if i.name in ("Pleamar", "Abisal")]
    monkeypatch.setattr(hk, "pick_presets", lambda: infos)
    hk.list.setCurrentRow(0)
    hk.list.item(0).setSelected(True)
    hk.enqueue_variants()
    assert len(captured) == 2
    names = [i.job.out_path.name for i in captured]
    assert any("[Pleamar]" in n for n in names) and any("[Abisal]" in n for n in names)
    pleamar = next(i for i in captured if "[Pleamar]" in i.job.out_path.name)
    assert pleamar.job.style.glow_gain == pytest.approx(1.6) and pleamar.job.style.font_frac == pytest.approx(0.076)
    assert e.style.glow_gain == 1.0  # el editor no cambió: las variantes aplican sobre copias


def test_variants_cancelled_dialog_enqueues_nothing(win, captured, monkeypatch):
    e = win.editor
    run_suggestions(e)
    monkeypatch.setattr(e._hooks, "pick_presets", lambda: [])
    e._hooks.list.item(0).setSelected(True)
    e._hooks.enqueue_variants()
    assert captured == []
