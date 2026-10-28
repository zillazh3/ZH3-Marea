"""Ventana de revisión, línea de tiempo y panel de Ajustes de la función experimental. El worker y el reproductor son simulados; lo real está en test_lab_real.py."""
import dataclasses
import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPoint, QSettings, Qt, Signal
from PySide6.QtTest import QTest

from marea.lab import env
from marea.project import project_from_ttml
from marea.ui import lab
from tests.test_lab_align_job import DEMO, fake_sidecar

ROOT = Path(__file__).resolve().parents[1]


class FakeRunner(QObject):
    stage = Signal(str)
    done = Signal(str, dict)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.calls, self.busy = [], False

    def ask(self, cmd, **params):
        self.calls.append((cmd, params))
        self.busy = True

    def cancel(self):
        self.calls.append(("cancel", {}))

    def close(self):
        pass

    def deliver(self, cmd, data):
        self.busy = False
        self.done.emit(cmd, data)


class FakePlayer(QObject):
    position = Signal(float)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.played = []

    def play(self, samples, start, end=None):
        self.played.append((start, end))

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture
def review(make_window, tmp_path, monkeypatch):
    ttml = tmp_path / "cancion.ttml"
    ttml.write_bytes(DEMO.read_bytes())
    win = make_window()
    project = project_from_ttml(ttml)
    project.audio_file = tmp_path / "audio.wav"
    project.audio_file.write_bytes(b"x")
    project.audio_samples = (0.3 * np.sin(np.arange(48000 * 60) / 20.0)).astype(np.float32).repeat(2).reshape(-1, 2)
    win.load_project(project)
    monkeypatch.setattr(lab, "prerequisites", lambda *a, **k: None)  # las precondiciones tienen su propio test
    runner, player = FakeRunner(), FakePlayer()
    rw = lab.LabReviewWindow(win.editor, runner=runner, player=player)
    rw.show()
    yield rw, runner, player, win, ttml
    rw.close()


def aligned(rw, runner, conf=0.05):
    rw.cb_lang.setCurrentIndex(rw.cb_lang.findData("es"))
    rw.btn_align.click()
    cmd, params = runner.calls[-1]
    assert cmd == "align"
    runner.deliver("align", fake_sidecar(rw.skeleton, lambda i: 0, conf=conf))
    return params


# ---------------- preparar y alinear ----------------

def test_aligning_a_ttml_project_sends_its_syllables_and_opens_the_review(review):
    rw, runner, _, _, _ = review
    assert rw.mode == "ttml" and not rw.ed_text.isVisibleTo(rw) and not rw.btn_asr.isVisibleTo(rw)
    params = aligned(rw, runner)
    assert params["lang"] == "es" and params["separate"] == "" and params["lines"][0]["words"][0] == ["Ca", "mi", "no"]
    assert rw.pages.currentIndex() == 1 and rw.session is not None
    assert rw.review_list.count() == len(rw.session.review_ids()) > 0
    assert "Requieren revisión" in rw.lb_review.text()
    assert rw.timeline.session is rw.session and len(rw.timeline.blocks()) > 50


def test_missing_prerequisites_block_the_request_with_a_plain_message(review, monkeypatch):
    rw, runner, _, _, _ = review
    monkeypatch.setattr(lab, "prerequisites", lambda *a, **k: "Falta instalar el entorno de sincronía.")
    rw.btn_align.click()
    assert runner.calls == [] and "Falta instalar" in rw.lb_progress.text()


def test_a_failure_or_cancel_from_the_sidecar_is_shown_and_unlocks_the_buttons(review):
    rw, runner, _, _, _ = review
    rw.btn_align.click()
    assert not rw.btn_align.isEnabled() and rw.btn_cancel.isVisibleTo(rw)
    rw.btn_cancel.click()
    assert runner.calls[-1][0] == "cancel"
    runner.cancelled.emit()
    assert rw.btn_align.isEnabled() and "Cancelado" in rw.lb_progress.text()
    rw.btn_align.click()
    runner.failed.emit("align", "No pude leer ese archivo de audio.")
    assert rw.btn_align.isEnabled() and rw.lb_progress.text() == "No pude leer ese archivo de audio."


def test_progress_text_shows_only_what_the_sidecar_reports(review):
    rw, runner, _, _, _ = review
    runner.stage.emit(lab.stage_text("separate", "start"))
    assert rw.lb_progress.text() == "Separando la voz…"
    runner.stage.emit(lab.stage_text("emissions", "cached"))
    assert "ya estaba hecho" in rw.lb_progress.text()
    assert lab.stage_text("align", "done", 2.34) == "Alineando la letra: listo en 2.3 s"


# ---------------- revisar y editar ----------------

def test_selecting_from_the_review_list_loads_the_unit_and_applying_new_times_fixes_it(review):
    rw, runner, _, _, _ = review
    aligned(rw, runner)
    n0 = rw.review_list.count()
    first = rw.review_list.item(0)
    rw.review_list.itemClicked.emit(first)
    uid = first.data(Qt.UserRole)
    kind, unit, _ = rw.session.find(uid)
    assert rw.sp_start.value() == unit.start and rw.sp_end.value() == unit.end and rw.btn_apply.isEnabled()
    rw.sp_end.setValue(unit.end - 20)
    rw.btn_apply.click()
    fixed = rw.session.find(uid)[1]
    assert fixed.end == unit.end - 20 and fixed.manual_override
    assert rw.review_list.count() == n0 - 1  # la corregida deja de estar «por revisar»


def test_an_invalid_edit_is_refused_with_the_reason(review):
    rw, runner, _, _, _ = review
    aligned(rw, runner)
    s0, s1 = rw.session.alignment.lines[0].words[0].syllables[:2]
    rw._select(s0.id)
    rw.sp_end.setValue(s1.end + 40)
    rw.btn_apply.click()
    assert "No se aplicó" in rw.lb_progress.text() and rw.session.find(s0.id)[1].end == s0.end


def test_dragging_a_block_edge_edits_the_unit(review):
    rw, runner, _, _, _ = review
    aligned(rw, runner)
    tl = rw.timeline
    syl = rw.session.alignment.lines[0].words[0].syllables[0]
    x, y = int(tl._x(syl.end)), tl._lane_top(2) + LANE_MID
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
    assert tl._drag and tl._drag["edge"] == "end"
    QTest.mouseMove(tl, QPoint(x - 6, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x - 6, y))
    new = rw.session.find(syl.id)[1]
    assert new.manual_override and syl.start + 10 <= new.end < syl.end and new.end % 10 == 0


def test_clicking_empty_space_deselects_and_double_click_plays_from_there(review):
    rw, runner, player, _, _ = review
    aligned(rw, runner)
    tl = rw.timeline
    syl = rw.session.alignment.lines[0].words[0].syllables[0]
    mid = int(tl._x((syl.start + syl.end) / 2))
    QTest.mouseClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mid, tl._lane_top(2) + LANE_MID))
    assert rw._selected == syl.id
    QTest.mouseClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(5, tl._lane_top(2) + LANE_MID))
    assert rw._selected == "" and not rw.btn_apply.isEnabled()
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mid, 30))
    assert player.played and abs(player.played[-1][0] - tl._ms(mid)) < 50


def test_listen_button_plays_the_selected_unit_with_a_little_context(review):
    rw, runner, player, _, _ = review
    aligned(rw, runner)
    s = rw.session.alignment.lines[1].words[0].syllables[0]
    rw._select(s.id)
    rw.btn_play.click()
    assert player.played[-1] == (max(0, s.start - 300), s.end + 500)
    player.position.emit(7000.0)
    assert rw.timeline._playhead == 7000.0


def test_global_offset_is_separate_from_units_and_undo_restores_it(review):
    rw, runner, _, _, _ = review
    aligned(rw, runner)
    times = [(s.start, s.end) for l in rw.session.alignment.lines for w in l.words for s in w.syllables]
    rw.sp_offset.setValue(180)
    rw.sp_offset.editingFinished.emit()
    assert rw.session.alignment.global_offset["ms"] == 180 and "manual" in rw.lb_offset.text()
    assert [(s.start, s.end) for l in rw.session.alignment.lines for w in l.words for s in w.syllables] == times
    rw.btn_undo.click()
    assert rw.session.alignment.global_offset["ms"] == 0 and rw.sp_offset.value() == 0


def test_realigning_a_section_asks_for_a_window_and_merges_the_answer(review):
    rw, runner, _, _, _ = review
    aligned(rw, runner)
    line = rw.session.alignment.lines[2]
    rw._select(line.words[0].syllables[0].id)
    rw.btn_realign.click()
    cmd, params = runner.calls[-1]
    assert cmd == "align" and params["lines"][0]["id"] == line.id and len(params["window"]) == 2 and params["window"][0] <= line.start
    reply = fake_sidecar(rw.skeleton, lambda i: 0, conf=0.9)
    reply["lines"] = [l for l in reply["lines"] if l["id"] == line.id]
    runner.deliver("align", reply)
    assert rw.pages.currentIndex() == 1 and "recalculada" in rw.lb_progress.text()
    assert rw.session.find(line.words[0].syllables[0].id)[1].confidence == 0.9


# ---------------- vista previa y exportación ----------------

def test_preview_loads_the_aligned_lyrics_in_the_editor_and_back_restores_the_original(review):
    rw, runner, _, win, _ = review
    aligned(rw, runner)
    original = win.editor.project
    rw.btn_preview.click()
    assert win.editor.project is not original and win.editor.project.lyrics is not original.lyrics
    assert len(win.editor.project.lyrics.lines) == len(original.lyrics.lines)
    rw.btn_back.click()
    assert win.editor.project is original


def test_approve_writes_a_new_ttml_next_to_the_original_and_never_touches_it(review):
    rw, runner, _, win, ttml = review
    digest = hashlib.sha256(ttml.read_bytes()).hexdigest()
    aligned(rw, runner)
    rw.btn_approve.click()
    out = ttml.with_name("cancion.aligned.ttml")
    assert out.exists() and out.with_suffix("").with_name("cancion.aligned.alignment.json").exists()
    assert hashlib.sha256(ttml.read_bytes()).hexdigest() == digest
    rw.btn_approve.click()
    assert ttml.with_name("cancion.aligned (2).ttml").exists()  # la segunda vez no pisa la primera


def test_a_saved_alignment_can_be_reopened_and_exported_against_its_original(review, tmp_path):
    rw, runner, _, _, ttml = review
    aligned(rw, runner)
    ttml2, js = rw.session.export(tmp_path, "guardado.ttml")
    rw.session = None
    rw.load_alignment(js)
    assert rw.session is not None and rw.pages.currentIndex() == 1
    assert rw.session.original == ttml.read_bytes()  # el sha del TTML coincide: se exporta reescribiendo el original
    ttml.write_bytes(ttml.read_bytes() + b" ")  # cualquier cambio del archivo cambia su hash
    rw.load_alignment(js)
    assert rw.session.original is None and "cambió" in rw.lb_progress.text()


# ---------------- Modo B: texto y borrador ----------------

def test_text_mode_transcribes_a_draft_then_aligns_the_edited_text(make_window, tmp_path, monkeypatch):
    win = make_window()
    win.load_demo()  # el demo no tiene TTML en disco: modo texto
    project = win.editor.project
    project.audio_file = tmp_path / "a.wav"
    project.audio_file.write_bytes(b"x")
    project.audio_samples = np.zeros((48000 * 5, 2), np.float32)
    monkeypatch.setattr(lab, "prerequisites", lambda *a, **k: None)
    runner = FakeRunner()
    rw = lab.LabReviewWindow(win.editor, runner=runner, player=FakePlayer())
    rw.show()
    assert rw.mode == "text" and rw.ed_text.isVisibleTo(rw) and rw.btn_asr.isVisibleTo(rw) and rw.ed_text.toPlainText().strip()
    rw.btn_asr.click()
    assert runner.calls[-1][0] == "asr"
    runner.deliver("asr", {"language": "es", "language_probability": 1.0, "lines": [
        {"text": "hola mundo", "start": 1000, "end": 2000, "suspect": False}, {"text": "ruido", "start": 3000, "end": 4000, "suspect": True}]})
    assert rw.ed_text.toPlainText() == "hola mundo\nruido" and rw._lang_code() == "es" and "1 sospechosas" in rw.lb_progress.text()
    rw.ed_text.setPlainText("hola mundo\nadiós")
    rw.btn_align.click()
    cmd, params = runner.calls[-1]
    assert cmd == "align" and params["lines"] == [{"id": "L0", "text": "hola mundo"}, {"id": "L1", "text": "adiós"}]
    runner.deliver("align", {"lines": [
        {"id": "L0", "start": 1000, "end": 2000, "pauses": [], "words": [{"text": "hola", "start": 1000, "end": 1400, "confidence": 0.9, "approximate": False, "syllables": []},
                                                                      {"text": "mundo", "start": 1500, "end": 2000, "confidence": 0.9, "approximate": False, "syllables": []}]},
        {"id": "L1", "start": 2600, "end": 3200, "pauses": [], "words": [{"text": "adiós", "start": 2600, "end": 3200, "confidence": 0.05, "approximate": False, "syllables": []}]}],
        "language": "es", "audio_sha256": "ab" * 32, "duration_ms": 5000, "model": "m", "device": {"device": "cpu"}})
    assert rw.session.text_editable and rw.ed_line.isEnabled() is False  # sin selección todavía
    rw._select("L1.W0")
    assert rw.ed_line.isEnabled() and rw.ed_line.text() == "adiós"
    rw.ed_line.setText("adiós amigo")
    rw.btn_realign.click()
    assert runner.calls[-1][1]["lines"] == [{"id": "L1", "text": "adiós amigo"}]
    rw.close()


# ---------------- precondiciones ----------------

def test_prerequisites_name_exactly_what_is_missing(tmp_path):
    root, audio = tmp_path / "lab", tmp_path / "a.wav"
    assert "audio" in lab.prerequisites(root, lang="es", separate=False, audio=None)
    audio.write_bytes(b"x")
    assert "instalar el entorno" in lab.prerequisites(root, lang="es", separate=False, audio=audio)
    py = env.env_python(root)
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    (root / "ready.json").write_text('{"requirements": "%s"}' % env.requirements_hash())
    assert "idioma" in lab.prerequisites(root, lang="", separate=False, audio=audio)
    assert "alineación (es)" in lab.prerequisites(root, lang="es", separate=False, audio=audio)
    snap = root / "hf" / "hub" / "models--jonatasgrosman--wav2vec2-large-xlsr-53-spanish" / "snapshots" / "x"
    snap.mkdir(parents=True)
    (snap / "pytorch_model.bin").write_bytes(b"1")
    assert lab.prerequisites(root, lang="es", separate=False, audio=audio) is None
    assert "separación de voz" in lab.prerequisites(root, lang="es", separate=True, audio=audio)
    assert "transcripción" in lab.prerequisites(root, lang="", separate=False, audio=audio, asr=True)


# ---------------- Ajustes ----------------

@pytest.fixture
def panel(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)
    settings.setValue(lab.LAB_DIR_KEY, str(tmp_path / "lab"))
    notes = []
    p = lab.LabSettingsPanel(settings, lambda text, kind="info", action=None: notes.append((text, kind)))
    p.confirm = lambda text: True
    yield p, notes, tmp_path / "lab"
    p.deleteLater()


def test_settings_panel_states_follow_what_is_on_disk(panel):
    p, _, root = panel
    assert p.btn_install.isEnabled() and not p.btn_uninstall.isEnabled() and "sin instalar" in p.status.text()
    assert all(not btn.isEnabled() for _, btn in p.rows.values())  # sin entorno no se puede descargar nada
    py = env.env_python(root)
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    (root / "ready.json").write_text('{"requirements": "%s"}' % env.requirements_hash())
    p.refresh()
    assert not p.btn_install.isEnabled() and p.btn_uninstall.isEnabled() and "instalado" in p.status.text()
    assert all(btn.isEnabled() and state.text() == "Pendiente" for state, btn in p.rows.values())


def test_install_asks_first_and_runs_in_the_background_reporting_real_lines(panel, monkeypatch):
    p, notes, root = panel
    p.confirm = lambda text: False
    ran = []
    monkeypatch.setattr(lab, "run_async", lambda fn, ok, err: ran.append(fn))
    p.install()
    assert ran == []  # sin confirmación no se descarga nada
    p.confirm = lambda text: True

    def fake_install(root_, progress, cancel):
        progress("torch", "Downloading torch (2.6GiB)")
        env.env_python(root_).parent.mkdir(parents=True)
        env.env_python(root_).write_bytes(b"")
        (root_ / "ready.json").write_text('{"requirements": "%s"}' % env.requirements_hash())

    monkeypatch.setattr(env, "install", fake_install)
    monkeypatch.setattr(lab, "run_async", lambda fn, ok, err: ok(fn()))
    p.install()
    assert ("Entorno instalado.", "ok") in notes and not p.btn_install.isEnabled() and not p.btn_cancel.isVisibleTo(p)
    assert isinstance(p._cancel, (threading.Event, type(None)))


def test_cancelling_an_install_is_reported_as_cancelled_not_as_an_error(panel, monkeypatch):
    p, notes, _ = panel

    def fake_install(root_, progress, cancel):
        raise env.InstallCancelled()

    monkeypatch.setattr(env, "install", fake_install)
    monkeypatch.setattr(lab, "run_async", lambda fn, ok, err: _call(fn, ok, err))
    p.install()
    assert notes[-1] == ("Cancelado.", "info")


def _call(fn, ok, err):
    try:
        ok(fn())
    except Exception as e:
        err(e)


def test_uninstall_removes_the_whole_lab_folder_after_asking(panel):
    p, notes, root = panel
    (root / "env").mkdir(parents=True)
    (root / "env" / "x.bin").write_bytes(b"1")
    p.confirm = lambda text: False
    p.uninstall()
    assert root.exists()
    p.confirm = lambda text: True
    p.uninstall()
    assert not root.exists() and notes[-1][1] == "ok"


# ---------------- con la función apagada no se carga nada ----------------

def test_with_the_feature_off_nothing_of_the_lab_is_imported_and_the_menu_entry_is_hidden(tmp_path):
    code = f"""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, {str(ROOT)!r})
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from marea.ui import theme
from marea.ui.window import MainWindow
from marea.ui.screens import SettingsBody
app = QApplication([]); theme.load_fonts(); theme.apply(app, "dark")
win = MainWindow(settings=QSettings({str(tmp_path / "off.ini")!r}, QSettings.IniFormat), mica=False)
win.load_demo(); win.show()
body = win.findChildren(SettingsBody)[0]
win.editor.menu.aboutToShow.emit()
print("OFF", [m for m in sys.modules if m.startswith(("marea.lab", "marea.ui.lab", "sidecar"))], win.editor.act_sync.isVisible(), body.cb_align.isChecked())
body.cb_align.setChecked(True)
win.editor.menu.aboutToShow.emit()
print("ON", "marea.ui.lab" in sys.modules, win.editor.act_sync.isVisible(), win.editor.settings.value("experimental/align", False, type=bool))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=ROOT)
    off = next(l for l in r.stdout.splitlines() if l.startswith("OFF")), next(l for l in r.stdout.splitlines() if l.startswith("ON"))
    assert off[0] == "OFF [] False False", (off, r.stderr[-500:])
    assert off[1] == "ON True True True"


LANE_MID = 10
