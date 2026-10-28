import subprocess
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEventLoop, QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export import ffmpeg
from marea.export.job import ExportJob, clip_range
from marea.render.scene import Style

DEMO_TTML = Path(__file__).parent.parent / "assets" / "demo" / "marea-de-luz.ttml"


@pytest.fixture
def win(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w


def wait(signal, timeout_ms=60_000):
    loop, got = QEventLoop(), []
    signal.connect(lambda *a: (got.append(a), loop.quit()))
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return got


def click_row(editor, row, shift=False, monkeypatch=None):
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: Qt.ShiftModifier if shift else Qt.NoModifier))
    editor._on_verse_clicked(editor.list.item(row))


def test_home_has_actions_and_editor_starts_empty(make_window):
    w = make_window()
    assert w.editor.stack.currentIndex() == 0 and not w.editor.btn_export.isEnabled()
    texts = [b.text() for b in w.pages.widget(0).findChildren(QPushButton)]
    assert "Abrir .ttml…" in texts and "Probar con la canción demo" in texts


def test_loading_demo_fills_editor(win):
    e = win.editor
    assert win.pages.currentIndex() == 3 and win.sidebar.current() == 3 and e.stack.currentIndex() == 1
    assert e.list.count() == 11 and (e.first, e.last) == (0, 5) and e.btn_export.isEnabled()
    assert win.titlebar.title.text().startswith("Demo · letra original – Marea de luz"[:20])
    assert e.range_label.text().startswith("Versos 1–6")
    # el preview quedó en el inicio del rango, con el motor asentado
    start, _ = clip_range(win.editor.project.lyrics, 0, 5)
    assert e.preview.scene.engine.t * 1000 == pytest.approx(start)


def test_click_then_shift_click_selects_range(win, monkeypatch):
    e = win.editor
    click_row(e, 2, monkeypatch=monkeypatch)
    assert (e.first, e.last) == (2, 2)
    click_row(e, 4, shift=True, monkeypatch=monkeypatch)
    assert (e.first, e.last) == (2, 4) and e.range_label.text().startswith("Versos 3–5")
    click_row(e, 0, shift=True, monkeypatch=monkeypatch)  # hacia atrás desde el ancla
    assert (e.first, e.last) == (0, 2)
    start, end = clip_range(e.project.lyrics, 0, 2)
    assert e.preview._range == (start, end)


def test_preview_plays_in_fixed_steps_and_loops_the_range(win, monkeypatch):
    e = win.editor
    click_row(e, 0, monkeypatch=monkeypatch)  # rango de un verso (3.5 s)
    start, end = clip_range(e.project.lyrics, 0, 0)
    e.preview.set_playing(True)
    QTest.qWait(400)
    t = e.preview.scene.engine.t * 1000
    assert start < t < end and e.preview.playing
    e.preview.seek(e.preview.duration - 20)  # casi al final: debe volver al inicio solo
    QTest.qWait(300)
    assert e.preview.scene.engine.t * 1000 < start + 400
    e.preview.set_playing(False)
    t0 = e.preview.scene.engine.t
    QTest.qWait(150)
    assert e.preview.scene.engine.t == t0  # en pausa el tiempo de la canción no avanza


def test_style_controls_drive_params_and_style(win):
    e = win.editor
    e.cb_vib.setChecked(False)
    e.cb_emph.setChecked(False)
    e.cb_bloom.setChecked(False)
    e.sl_blur.setValue(0)
    e.sl_wob.setValue(70)
    assert (e.params.vib, e.params.emph, e.params.blurm, e.params.wob) == (False, False, 0.0, 0.7)
    assert e.preview.scene.style.bloom is False and e.preview.scene.engine.p is e.params  # el preview usa los mismos objetos
    e.ed_title.setText("Otra")
    e.ed_artist.setText("Alguien")
    assert win.titlebar.title.text() == "Alguien – Otra" and e.preview.scene.style.title == "Otra"


def test_motion_controls_drive_params_and_reach_the_job(win, monkeypatch, tmp_path):
    e = win.editor
    e.cmb_motion.setCurrentIndex(e.cmb_motion.findData("rebote"))
    e.sl_amt.setValue(60)
    e.cb_reduce.setChecked(False)
    assert (e.params.motion, e.params.motion_amt, e.params.reduce) == ("rebote", 0.6, False)
    assert e.lb_amt.text() == "60 %" and e.preview.scene.engine.p is e.params
    e.cb_reduce.setChecked(True)
    e.settings.setValue("output_dir", str(tmp_path))
    assert e.build_job().params.reduce is True  # el export respeta lo mismo que el preview, no el sistema


def test_reduced_motion_starts_from_the_system_setting(make_window, qapp, monkeypatch):
    import marea.ui.editor as ed
    monkeypatch.setattr(ed, "system_reduces_motion", lambda: True)
    w = make_window()
    assert w.editor.cb_reduce.isChecked() and w.editor.params.reduce is True


def test_system_reduces_motion_is_a_bool_and_safe_off_windows(monkeypatch):
    import marea.ui.editor as ed
    assert isinstance(ed.system_reduces_motion(), bool)
    monkeypatch.setattr(ed.sys, "platform", "linux")
    assert ed.system_reduces_motion() is False


def test_build_job_uses_range_preset_and_copies_settings(win, monkeypatch, tmp_path):
    e = win.editor
    e.settings.setValue("output_dir", str(tmp_path))
    e.settings.setValue("export/preset", "wide")
    e.settings.setValue("export/marker", "flash")
    click_row(e, 1, monkeypatch=monkeypatch)
    click_row(e, 3, shift=True, monkeypatch=monkeypatch)
    job = e.build_job()
    assert job.size == (1920, 1080) and job.fps == 60 and job.marker == "flash" and job.encoder == "cpu"
    assert (job.start_ms, job.end_ms) == clip_range(e.project.lyrics, 1, 3)
    assert job.out_path == tmp_path / "Demo · letra original - Marea de luz (versos 2–4) [ZH3 Marea].mp4"
    e.cb_vib.setChecked(False)
    assert job.params.vib is True  # el job lleva una copia: cambios posteriores no lo afectan


def test_open_ttml_valid_and_invalid(make_window, qapp, monkeypatch, tmp_path):
    w = make_window()
    w.show()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    w.open_ttml()  # cancelar el diálogo no hace nada
    assert w.editor.project is None and w.toasts.count() == 0
    bad = tmp_path / "malo.ttml"
    bad.write_text("esto no es xml", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad), "")))
    w.open_ttml()
    assert w.editor.project is None and w.toasts.count() == 1  # error claro, sin diálogo modal
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(DEMO_TTML), "")))
    w.open_ttml()
    assert w.editor.project.title == "marea-de-luz" and w.editor.list.count() == 11 and w.pages.currentIndex() == 3


def test_replace_ttml_keeps_cover_title_and_artist(make_window, qapp, monkeypatch, tmp_path):
    w = make_window()
    w.load_demo()
    ed = w.editor
    cover, title, artist = ed.project.cover, ed.project.title, ed.project.artist
    bad = tmp_path / "malo.ttml"
    bad.write_text("esto no es xml", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad), "")))
    n = ed.list.count()
    ed._replace_ttml()
    assert ed.list.count() == n and w.toasts.count() == 1  # inválido: letra intacta + error
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(DEMO_TTML), "")))
    ed._replace_ttml()
    p = ed.project
    assert (p.cover, p.title, p.artist) == (cover, title, artist) and p.source == DEMO_TTML and ed.list.count() == 11


# ---------------- export por la cola, modos y audio ----------------

import numpy as np  # noqa: E402

from marea.audio import local  # noqa: E402
from marea.export import queue as queue_mod  # noqa: E402
from marea.export.queue import CANCELLED, DONE, FAILED, QUEUED, RENDER, QueueItem  # noqa: E402
from marea.paths import app_data  # noqa: E402
from marea.ui.exports import ExportsPage, ExportRow  # noqa: E402
from marea.ui.services import Services  # noqa: E402
from tests.test_spotify_ui import FakeLyrics, FakeSpotify, T, until  # noqa: E402


@pytest.fixture
def captured(win, monkeypatch):
    """Intercepta la cola: se comprueba qué se encola sin renderizar nada."""
    items = []
    monkeypatch.setattr(win.queue, "add", items.append)
    return items


def set_mode(editor, mode):
    editor.mode_actions[mode].setChecked(True)
    editor.settings.setValue("export/mode", mode)
    editor._update_summary()


def test_lyrics_export_enqueues_a_job_with_range_and_no_audio(win, captured, tmp_path):
    e = win.editor
    e.settings.setValue("output_dir", str(tmp_path))
    e.settings.setValue("export/preset", "square")
    got = []
    e.enqueued.connect(got.append)
    e.preview.set_playing(True)
    e.export()
    (item,) = captured
    assert item.audio is None and item.job.size == (1080, 1080) and (item.job.start_ms, item.job.end_ms) == clip_range(e.project.lyrics, 0, 5)
    assert item.job.out_path.parent == tmp_path and got == [item] and not e.preview.playing
    assert e.summary.text().startswith("Solo letra")


def test_song_mode_needs_an_audio_source_and_explains_how(win, captured):
    e = win.editor
    set_mode(e, "song")
    e.cmb_source.setCurrentIndex(0)  # loopback, pero la demo no viene de Spotify
    e.export()
    assert not captured and win.toasts.count() == 1
    e.cmb_source.setCurrentIndex(1)
    e.export()
    assert not captured and win.toasts.count() == 2  # archivo sin elegir


def test_song_mode_with_local_audio_and_offset(win, captured):
    e = win.editor
    set_mode(e, "song")
    e.cmb_source.setCurrentIndex(1)
    e.project.audio_samples = np.zeros((local.RATE * 60, 2), np.float32)
    e.sl_offset.setValue(250)
    e.export()
    (item,) = captured
    assert item.audio.kind == "local" and item.audio.offset_ms == 250 and (item.audio.start_ms, item.audio.end_ms) == (item.job.start_ms, item.job.end_ms)
    assert item.audio.samples is e.project.audio_samples


def test_outro_setting_extends_the_clip_and_fades_the_audio(win, captured):
    e = win.editor
    set_mode(e, "song")
    e.cmb_source.setCurrentIndex(1)
    e.project.audio_samples = np.zeros((local.RATE * 60, 2), np.float32)
    e.settings.setValue("export/outro_ms", "4000")
    e.export()
    (item,) = captured
    assert item.job.end_ms == clip_range(e.project.lyrics, e.first, e.last, 4000)[1] and item.audio.fade_out_ms == 4000


def test_song_mode_with_spotify_uses_device_offset_and_track(make_window, qapp, tmp_path):
    sp = FakeSpotify()
    services = Services(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat), spotify=sp, lyrics=FakeLyrics(), poll_ms=60_000)
    w = make_window(services=services)
    w.show()
    items = []
    w.queue.add = items.append
    w.open_track(T(3, "Con Spotify"))
    assert until(lambda: w.editor.project is not None)
    e = w.editor
    assert e.cmb_source.currentData() == "spotify"  # hay pista de Spotify y cuenta conectada: es la opción por defecto
    e.settings.setValue("audio/device", "Mi interfaz")
    e.sl_offset.setValue(-40)
    set_mode(e, "song")
    e.export()
    (item,) = items
    assert (item.audio.kind, item.audio.track.id, item.audio.device, item.audio.offset_ms) == ("spotify", "id3", "Mi interfaz", -40)
    items.clear()
    set_mode(e, "full")
    e.export()
    (full,) = items
    assert (full.job.start_ms, full.job.end_ms) == (0.0, 183000.0)  # canción completa: de 0 a la duración de la pista
    assert full.job.out_path.name == "Artista - Con Spotify (completa) [ZH3 Marea].mp4"
    assert (full.audio.start_ms, full.audio.end_ms) == (0.0, 183000.0)


def test_spotify_source_links_what_is_playing_without_losing_the_selection(make_window, qapp, tmp_path):
    from marea.providers.spotify import Playback
    sp = FakeSpotify()
    services = Services(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat), spotify=sp, lyrics=FakeLyrics(), poll_ms=60_000)
    w = make_window(services=services)
    w.show()
    w.load_demo()  # proyecto sin pista de Spotify
    items = []
    w.queue.add = items.append
    e = w.editor
    wav = Path("cancion.wav")
    e.project.audio_file, e.project.audio_samples = wav, np.zeros((local.RATE * 60, 2), np.float32)
    set_mode(e, "song")
    e.cmb_source.setCurrentIndex(1)
    # Spotify elegido sin nada sonando: mensaje explícito, no se exporta y no se pierde nada
    e.cmb_source.setCurrentIndex(0)
    assert "No hay nada sonando" in e.lb_audio.text() and e.project.audio_file == wav and e.list.count() == 11
    e.export()
    assert not items and w.toasts.count() == 1
    # empieza a sonar algo: se vincula sola
    assert until(lambda: not services._polling)  # la consulta que lanzó elegir Spotify ya terminó
    sp.playing = Playback(T(7, "Suena"), 1000, True, 0)
    services.poll()
    assert until(lambda: e._linked is not None)
    assert "«Artista – Suena»" in e.lb_audio.text() and e.project.track is None
    e.export()
    (item,) = items
    assert (item.audio.kind, item.audio.track.id) == ("spotify", "id7")
    # cambia la canción en Spotify: lo vinculado se queda
    sp.playing = Playback(T(8, "Otra"), 0, True, 0)
    services.poll()
    assert until(lambda: services.playback.track.id == "id8") and e._linked.id == "id7"
    # nada se tocó del proyecto ni del archivo local
    e.cmb_source.setCurrentIndex(1)
    assert "cancion.wav" in e.lb_audio.text() and e.project.audio_samples is not None
    # otro proyecto: la vinculación no se arrastra
    w.load_demo()
    assert e._linked is None


def test_spotify_source_without_account_explains_and_keeps_the_local_file(win):
    e = win.editor
    e.project.audio_file = Path("x.wav")
    e.cmb_source.setCurrentIndex(0)
    assert "Conecta tu cuenta" in e.lb_audio.text() and e.project.audio_file == Path("x.wav")


def test_missing_ffmpeg_is_reported_with_a_fix(win, captured, monkeypatch):
    def boom(name="ffmpeg"):
        raise FileNotFoundError("No encontré ffmpeg. Instálalo o ponlo en la carpeta ffmpeg/ junto a la app.")
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", boom)
    win.editor.export()
    assert not captured and win.toasts.count() == 1


def test_audio_tab_loads_a_file_and_shows_the_waveform(win, monkeypatch, tmp_path):
    e = win.editor
    t = np.arange(local.RATE * 30) / local.RATE
    tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    wav = tmp_path / "a.wav"
    local.write_wav(wav, np.stack([tone, tone], axis=1))
    e.cmb_source.setCurrentIndex(1)
    assert not e.btn_audio_file.isHidden() and e.wave._samples is None
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(wav), "")))
    e._choose_audio()
    assert until(lambda: e.project.audio_samples is not None)
    assert e.project.audio_file == wav and "a.wav" in e.lb_audio.text()
    start, end = clip_range(e.project.lyrics, 0, 5)
    assert e.wave._range == (start, end) and e.wave._offset == 0 and e.wave._markers  # marcadores = inicio de cada verso
    e.sl_offset.setValue(-300)
    assert e.project.audio_offset_ms == -300 and e.wave._offset == -300
    assert e.wave.peaks(100).max() > 0.4
    e.wave.grab()  # pinta sin excepciones
    # elegir un archivo inválido da un mensaje claro y no rompe el proyecto
    bad = tmp_path / "x.mp3"
    bad.write_bytes(b"no es audio")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad), "")))
    before = win.toasts.count()
    e._choose_audio()
    assert until(lambda: win.toasts.count() >= before + 2) and e.project.audio_file == wav


def test_loopback_offset_is_a_global_setting(win):
    e = win.editor
    e.cmb_source.setCurrentIndex(0)
    e.sl_offset.setValue(120)
    assert float(e.settings.value("audio/loopback_offset_ms")) == 120 and e.sl_offset.minimum() == -500
    e.cmb_source.setCurrentIndex(1)
    assert e.sl_offset.minimum() == -3000 and e.sl_offset.value() == 0  # el del archivo es otro, por proyecto


# ---------------- página de Exportaciones ----------------

def make_item(tmp_path, name="a.mp4"):
    job = ExportJob(exact_lyrics(), b"", tmp_path / name, 0, 1000, size=(180, 320), fps=30, prefer_gpu=False)
    return QueueItem("Canción", "Artista", bytes(demo_cover(32).encodeToData()), job)


def test_exports_page_lists_rows_progress_and_actions(win, monkeypatch, tmp_path):
    q, page = win.queue, win.pages.widget(4)
    monkeypatch.setattr(q, "_pump", lambda: None)  # no arrancar renders reales
    assert isinstance(page, ExportsPage) and page.stack.currentWidget() is page.empty and not page.btn_clear.isEnabled()
    a, b = make_item(tmp_path, "a.mp4"), make_item(tmp_path, "b.mp4")
    q.add(a)
    q.add(b)
    assert page.stack.currentWidget() is page.scroll and len(page.rows) == 2
    ra = page.rows[a]
    assert [x.text() for x in ra.findChildren(QPushButton)] == ["Cancelar"] and ra.sub.text() == "En cola"
    a.state, a.stage, a.progress, a.eta = RENDER, "Renderizando video…", 0.42, 75.0
    q.changed.emit()
    q.item_changed.emit(a)
    assert ra.bar.value() == 420 and ra.eta.text() == "quedan 1:15" and "Renderizando" in ra.sub.text()
    a.state, a.stage, a.progress, a.eta = DONE, "Listo", 1.0, None
    b.state, b.error = FAILED, "ffmpeg no pudo codificar."
    q.changed.emit()
    page.rebuild()
    assert [x.text() for x in page.rows[a].findChildren(QPushButton)] == ["Abrir carpeta", "Copiar ruta", "Quitar"]
    assert page.rows[b].sub.text() == "ffmpeg no pudo codificar." and not page.rows[b].bar.isVisibleTo(page)
    QApplication.clipboard().clear()
    next(x for x in page.rows[a].findChildren(QPushButton) if x.text() == "Copiar ruta").click()
    assert QApplication.clipboard().text() == str(a.out_path)
    next(x for x in page.rows[b].findChildren(QPushButton) if x.text() == "Quitar").click()
    assert b not in q.items and b not in page.rows
    page.btn_clear.click()
    assert q.items == [] and page.stack.currentWidget() is page.empty


def test_cancelling_a_running_export_asks_first_but_a_queued_one_does_not(win, monkeypatch, tmp_path):
    q, page = win.queue, win.pages.widget(4)
    monkeypatch.setattr(q, "_pump", lambda: None)
    running, waiting = make_item(tmp_path, "r.mp4"), make_item(tmp_path, "w.mp4")
    q.add(running)
    q.add(waiting)
    running.state = RENDER
    q.changed.emit()
    cancels = []
    monkeypatch.setattr(q._runner, "cancel", lambda: cancels.append(1))
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.text()) or QMessageBox.No)
    next(x for x in page.rows[running].findChildren(QPushButton) if x.text() == "Cancelar").click()
    assert asked and not cancels  # respondió «seguir exportando»
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    next(x for x in page.rows[running].findChildren(QPushButton) if x.text() == "Cancelar").click()
    assert cancels == [1]
    asked.clear()
    next(x for x in page.rows[waiting].findChildren(QPushButton) if x.text() == "Cancelar").click()
    assert not asked and waiting.state == CANCELLED  # lo que aún no empezó se cancela sin diálogo


# ---------------- Inicio: clips recientes ----------------

def test_home_shows_recent_clips_and_opens_their_folder(make_window, qapp, monkeypatch, tmp_path):
    from marea.export import history
    from marea.ui import home as home_mod
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    thumb = tmp_path / "t.png"
    thumb.write_bytes(bytes(demo_cover(64).encodeToData()))
    history.add({"path": str(video), "title": "Mi clip", "artist": "Yo", "thumb": str(thumb)})
    w = make_window()
    w.show()
    home = w.pages.widget(0)
    assert home.recents.isVisibleTo(home) and home.recents_title.text() == "Clips recientes"
    opened = []
    monkeypatch.setattr(home_mod, "reveal_in_folder", lambda p: opened.append(p))
    from PySide6.QtWidgets import QPushButton as Btn
    next(b for b in home.recents.findChildren(Btn)).click()
    assert opened == [str(video)]
    (app_data() / "history.json").unlink()  # sin historial no hay sección
    home.reload_recents()
    assert not home.recents_title.isVisibleTo(home)


# ---------------- Ajustes de audio ----------------

def test_audio_settings_persist_and_probe_reports_clearly(win, monkeypatch):
    from marea.ui import screens
    from marea.ui.screens import SettingsBody
    body = win.pages.widget(5).findChild(SettingsBody)
    assert body.device.itemData(0) == "" and "automático" in body.device.itemText(0)
    body.offset.setValue(80)
    assert float(win.settings.value("audio/loopback_offset_ms")) == 80
    if body.device.count() > 1:
        body.device.setCurrentIndex(1)
        assert win.settings.value("audio/device") == body.device.itemData(1)
    before = win.toasts.count()
    monkeypatch.setattr(screens, "probe_level", lambda name: 0.0)
    body._probe()
    assert until(lambda: win.toasts.count() >= before + 2 and body.btn_probe.isEnabled())  # «Escuchando…» + el resultado
    monkeypatch.setattr(screens, "probe_level", lambda name: 0.4)
    before = win.toasts.count()
    body._probe()
    assert until(lambda: win.toasts.count() >= before + 2 and body.btn_probe.isEnabled())


# ---------------- Desfase automático ----------------

def auto_setup(win, source, samples=None):
    e = win.editor
    e.cmb_source.setCurrentIndex(e.cmb_source.findData(source))
    if samples is not None:
        e.project.audio_samples = samples
        e._refresh_audio_tab()
    return e


def run_auto(win, e):
    before = win.toasts.count()
    e.btn_auto.click()
    assert until(lambda: e.btn_auto.isEnabled() and win.toasts.count() >= before + 2)


def test_auto_offset_local_file_sets_the_slider_and_the_project(win):
    from tests.test_offset import RATE, synth
    audio = synth(win.editor.project.lyrics, 230)
    e = auto_setup(win, "local", np.stack([audio, audio], axis=1))
    run_auto(win, e)
    assert e.sl_offset.value() == pytest.approx(230, abs=8) and e.project.audio_offset_ms == pytest.approx(230, abs=8)
    assert e.lb_offset.text().startswith("+2") and e.btn_auto.isEnabled()


def test_auto_offset_without_file_or_with_unrelated_audio_changes_nothing_and_explains(win):
    from tests.test_offset import RATE, synth
    e = auto_setup(win, "local")
    before = win.toasts.count()
    e.btn_auto.click()
    assert win.toasts.count() == before + 1 and e.project.audio_offset_ms == 0  # «elige primero un archivo»
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.1, RATE * 60).astype(np.float32)  # un archivo que no es la canción
    e = auto_setup(win, "local", np.stack([noise, noise], axis=1))
    run_auto(win, e)
    assert e.project.audio_offset_ms == 0 and e.sl_offset.value() == 0


def test_auto_offset_with_spotify_records_a_clip_and_stores_the_loopback_offset(win, monkeypatch):
    from marea.ui import editor as editor_mod
    from marea.export.job import clip_range
    from tests.test_offset import RATE, synth
    e = auto_setup(win, "spotify")
    monkeypatch.setattr(e, "_spotify_ready", lambda: True)
    full = synth(e.project.lyrics, 120)
    calls = []

    def fake_record(client, track, start, end, **kw):
        calls.append((start, end, kw))
        return np.stack([full[int(start / 1000 * RATE):int(end / 1000 * RATE)]] * 2, axis=1), RATE

    monkeypatch.setattr(editor_mod, "record_song_range", fake_record)
    run_auto(win, e)
    start, end = clip_range(e.project.lyrics, e.first, e.last)
    (got_start, got_end, kw), = calls
    assert got_start == start and got_end <= min(end, start + editor_mod.AUTO_RECORD_MS)
    assert kw["offset_ms"] == 0.0  # se mide sin corregir: lo medido es justo lo que hay que compensar
    assert float(win.settings.value("audio/loopback_offset_ms")) == pytest.approx(120, abs=8)
    assert e.sl_offset.value() == pytest.approx(120, abs=8)


def test_auto_offset_with_spotify_refuses_while_an_export_is_running_and_ignores_offsets_beyond_the_range(win, monkeypatch):
    from marea.ui import editor as editor_mod
    from tests.test_offset import RATE, synth
    e = auto_setup(win, "spotify")
    monkeypatch.setattr(e, "_spotify_ready", lambda: True)
    monkeypatch.setattr(editor_mod, "record_song_range", lambda *a, **k: pytest.fail("no debía grabar"))
    e.queue._active = object()  # hay una exportación en curso
    before = win.toasts.count()
    e.btn_auto.click()
    assert win.toasts.count() == before + 1 and e.btn_auto.isEnabled()
    e.queue._active = None
    full = synth(e.project.lyrics, 400)  # cerca del borde del rango del loopback (±500), pero dentro
    monkeypatch.setattr(editor_mod, "record_song_range", lambda client, track, s, en, **k: (
        np.stack([full[int(s / 1000 * RATE):int(en / 1000 * RATE)]] * 2, axis=1), RATE))
    run_auto(win, e)
    assert e.sl_offset.value() == pytest.approx(400, abs=8)
    e.settings.setValue("audio/loopback_offset_ms", 0)
    e._refresh_audio_tab()
    full2 = synth(e.project.lyrics, 1500)  # fuera del rango del loopback (±500): no se aplica nada
    monkeypatch.setattr(editor_mod, "record_song_range", lambda client, track, s, en, **k: (
        np.stack([full2[int(s / 1000 * RATE):int(en / 1000 * RATE)]] * 2, axis=1), RATE))
    run_auto(win, e)
    assert float(win.settings.value("audio/loopback_offset_ms")) == 0


def test_settings_loopback_offset_follows_the_editor_when_reopened(win):
    from marea.ui.screens import SettingsBody
    body = win.pages.widget(5).findChild(SettingsBody)
    win.settings.setValue("audio/loopback_offset_ms", 135)
    win.pages.set_page(0)
    win.pages.set_page(5)
    assert body.offset.value() == 135


DEVICES = [{"name": "Altavoces", "loopback": True}, {"name": "Cable Input", "loopback": True},
           {"name": "Voicemeeter Out B1", "loopback": False}]


def audio_body(win, monkeypatch, devices=DEVICES):
    from marea.ui import screens
    from marea.ui.screens import SettingsBody
    monkeypatch.setattr(screens, "list_capture_devices", lambda: devices)
    body = win.pages.widget(5).findChild(SettingsBody)
    body._fill_devices(win.settings.value("audio/device", ""))
    return screens, body


def test_refreshing_the_device_list_keeps_the_saved_choice(win, monkeypatch):
    _, body = audio_body(win, monkeypatch)
    body.device.setCurrentIndex(body.device.findData("Cable Input"))
    assert win.settings.value("audio/device") == "Cable Input"
    body._fill_devices("Cable Input", DEVICES + [{"name": "USB nuevo", "loopback": True}])  # reaparece la lista completa
    assert win.settings.value("audio/device") == "Cable Input" and body.device.currentData() == "Cable Input"
    assert body.device.findData("USB nuevo") >= 0
    assert "entrada" in body.device.itemText(body.device.findData("Voicemeeter Out B1"))


def test_reopening_settings_picks_up_devices_that_appeared_meanwhile(win, monkeypatch):
    _, body = audio_body(win, monkeypatch)
    body.device.setCurrentIndex(body.device.findData("Altavoces"))
    assert body.device.findData("USB nuevo") < 0
    monkeypatch.setattr("marea.ui.screens.list_capture_devices", lambda: DEVICES + [{"name": "USB nuevo", "loopback": True}])
    win.pages.set_page(0)
    win.pages.set_page(5)  # volver a Ajustes
    assert until(lambda: body.device.findData("USB nuevo") >= 0)
    assert body.device.currentData() == "Altavoces" and win.settings.value("audio/device") == "Altavoces"


def test_a_saved_device_that_disappeared_is_shown_as_unavailable_not_as_automatic(win, monkeypatch):
    win.settings.setValue("audio/device", "Interfaz desconectada")
    _, body = audio_body(win, monkeypatch)
    assert body.device.currentData() == "Interfaz desconectada" and "no disponible" in body.device.currentText()
    assert win.settings.value("audio/device") == "Interfaz desconectada"  # llenar la lista no borra el ajuste


def test_detect_selects_and_saves_the_device_with_signal(win, monkeypatch):
    screens, body = audio_body(win, monkeypatch)
    monkeypatch.setattr(screens, "scan_levels", lambda: [{**DEVICES[1], "peak": 0.5}, {**DEVICES[0], "peak": 0.2},
                                                         {**DEVICES[2], "peak": 0.0}])
    before = win.toasts.count()
    body._detect()
    assert until(lambda: win.toasts.count() >= before + 2 and body.btn_detect.isEnabled())
    assert body.device.currentData() == "Cable Input" and win.settings.value("audio/device") == "Cable Input"
    # ya estaba seleccionado: detectar de nuevo sigue guardando el ajuste
    win.settings.setValue("audio/device", "")
    body._detect()
    assert until(lambda: win.settings.value("audio/device") == "Cable Input")


def test_detect_reports_silence_without_changing_the_choice(win, monkeypatch):
    screens, body = audio_body(win, monkeypatch)
    win.settings.setValue("audio/device", "Altavoces")
    body.device.setCurrentIndex(body.device.findData("Altavoces"))
    monkeypatch.setattr(screens, "scan_levels", lambda: [{**DEVICES[0], "peak": 0.0}])
    before = win.toasts.count()
    body._detect()
    assert until(lambda: win.toasts.count() >= before + 2 and body.btn_detect.isEnabled())
    assert win.settings.value("audio/device") == "Altavoces"
