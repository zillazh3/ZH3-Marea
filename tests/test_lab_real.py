"""Modo A de punta a punta con el worker REAL, el modelo y audio real (JamendoLyrics). Opt-in: se SALTA, con motivo, si falta algo.

    $env:MAREA_LAB_DIR = "<carpeta lab con el entorno instalado>"; pytest tests/test_lab_real.py -s

La letra de entrada es un TTML «mal sincronizado» (cada línea corrida al azar hasta ±300 ms respecto a la anotación manual): el
alineador debe acercarla a la anotación, y todo el recorrido (TTML → petición → sidecar → Alignment → TTML nuevo) es el del producto.
"""
import os
import random
import sys
from pathlib import Path

import pytest

from marea.export.ffmpeg import find_ffmpeg
from marea.lab import env
from marea.lab.align_job import apply_result, needs_review, request_lines
from marea.lab.alignment import validate
from marea.lab.metrics import word_errors
from marea.lab.sidecar import SidecarClient, worker_argv
from marea.lab.ttml_in import from_ttml
from marea.lab.ttml_out import clock, export_ttml
from marea.model.lyrics import parse_ttml
from tests.test_spotify_ui import until

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "local_data" / "jamendo"
LAB = Path(os.environ["MAREA_LAB_DIR"]) if os.environ.get("MAREA_LAB_DIR") else None

pytestmark = [
    pytest.mark.skipif(LAB is None or not env.env_python(LAB).exists(),
                       reason="Prueba real opt-in: define MAREA_LAB_DIR con el entorno del sidecar instalado (marea.lab.env.install)."),
    pytest.mark.skipif(not (DATA / "es" / "mp3" / "Baila_-_Alfonso_Lugo.mp3").exists() or not (DATA / "en" / "mp3" / "JASON_MILLER_-_CROWD_PLEASER.mp3").exists(),
                       reason="Faltan los audios de JamendoLyrics en tests/local_data/jamendo (licencias CC NC/ND: no se commitean)."),
]


def sloppy_ttml(lang: str, stem: str, jitter_ms: int, seed: int = 7) -> tuple[bytes, list[tuple[int, int]]]:
    """(TTML con cada línea corrida al azar, tiempos reales de cada palabra)."""
    import csv
    lines = [l.split() for l in (DATA / lang / "lyrics" / f"{stem}.txt").read_text(encoding="utf-8").splitlines() if l.split()]
    truth = [(round(float(r["word_start"]) * 1000), round(float(r["word_end"]) * 1000))
             for r in csv.DictReader(open(DATA / lang / "annotations" / "words" / f"{stem}.csv", encoding="utf-8"))]
    rng, ps, k = random.Random(seed), [], 0
    for words in lines:
        t = truth[k:k + len(words)]
        k += len(words)
        shift = rng.randint(-jitter_ms, jitter_ms)
        spans = " ".join(f'<span begin="{clock(max(0, a + shift))}" end="{clock(max(1, b + shift))}">{w}</span>' for w, (a, b) in zip(words, t))
        ps.append(f'<p begin="{clock(max(0, t[0][0] + shift))}" end="{clock(max(1, t[-1][1] + shift))}">{spans}</p>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?><tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
           f'itunes:timing="Word" xml:lang="{lang}"><body><div>' + "".join(ps) + "</div></body></tt>")
    return xml.encode("utf-8"), truth


@pytest.fixture
def worker(qapp):
    client = SidecarClient()
    done = {}
    client.ready.connect(lambda: done.update(ready=True))
    client.result.connect(lambda rid, data: done.update(result=data))
    client.failed.connect(lambda rid, msg, detail: done.update(failed=(msg, detail)))
    client.start(worker_argv(str(env.env_python(LAB))), extra_env={"HF_HOME": str(LAB / "hf")})
    assert until(lambda: "ready" in done, 30000), client.stderr_tail
    yield client, done
    client.stop()


def test_mode_a_end_to_end_improves_a_sloppy_ttml_and_exports_a_valid_one(worker, tmp_path):
    client, done = worker
    stem, lang = "Baila_-_Alfonso_Lugo", "es"
    original, truth = sloppy_ttml(lang, stem, 300)
    al = from_ttml(original, 0)
    before = word_errors([(s.start, s.end) for l in al.lines for w in l.words for s in w.syllables], truth)

    client.request("align", audio=str(DATA / lang / "mp3" / f"{stem}.mp3"), ffmpeg=find_ffmpeg(), cache_dir=str(tmp_path / "cache"),
                   lang=lang, lines=request_lines(al), use_energy=True, syllables=True)
    assert until(lambda: "result" in done or "failed" in done, 180000), client.stderr_tail
    assert "failed" not in done, done.get("failed")

    out = apply_result(al, done["result"])
    exported = export_ttml(out, original)  # valida adentro; levanta si hay algo mal
    lyrics = parse_ttml(exported)
    pred = [(s.begin, s.end) for l in lyrics.lines for s in l.syllables]
    after = word_errors(pred, truth)
    print(f"\nTTML mal sincronizado: inicio MAE {before['start']['mae']} ms, ±100 ms {before['start']['within100']:.0%}")
    print(f"tras alinear (energía): inicio MAE {after['start']['mae']} ms, ±100 ms {after['start']['within100']:.0%}; fin ±100 ms {after['end']['within100']:.0%}")
    print(f"revisión: {len(needs_review(out))}/{len(truth)} palabras; desfase global {out.global_offset}; dispositivo {done['result']['device']['device']}")

    assert validate(out) == []
    assert [l.text for l in lyrics.lines] == [l.text for l in parse_ttml(original).lines]  # el texto no cambió
    assert after["start"]["mae"] < before["start"]["mae"] / 2
    assert after["start"]["within100"] >= 0.85


def ask(client, done, cmd, timeout=180000, **params):
    done.pop("result", None), done.pop("failed", None)
    client.request(cmd, **params)
    assert until(lambda: "result" in done or "failed" in done, timeout), client.stderr_tail


def test_inference_never_touches_the_network(qapp, tmp_path):
    """El endpoint de Hugging Face apunta a un puerto local que cuenta conexiones: si la carga del modelo o del vocabulario saliera a la red,
    habría al menos una (con carga online, transformers/huggingface_hub consultan el Hub aunque el modelo esté en caché)."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    hits = []

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                hits.append(conn.recv(512).decode("latin-1").splitlines()[0])  # primera línea de la petición: qué pidió
            except OSError:
                hits.append("(sin datos)")
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    client, done = SidecarClient(), {}
    client.result.connect(lambda rid, data: done.update(result=data))
    client.failed.connect(lambda rid, msg, detail: done.update(failed=(msg, detail)))
    client.ready.connect(lambda: done.update(ready=True))
    client.start(worker_argv(str(env.env_python(LAB))), extra_env={"HF_HOME": str(LAB / "hf"), "HF_ENDPOINT": f"http://127.0.0.1:{srv.getsockname()[1]}"})
    try:
        assert until(lambda: "ready" in done, 30000)
        stem, lang = "Baila_-_Alfonso_Lugo", "es"
        original, _ = sloppy_ttml(lang, stem, 300)
        ask(client, done, "align", audio=str(DATA / lang / "mp3" / f"{stem}.mp3"), ffmpeg=find_ffmpeg(), cache_dir=str(tmp_path / "cache"), lang=lang,
            lines=request_lines(from_ttml(original, 0)))
        assert "failed" not in done, done.get("failed")
        assert hits == [], f"la alineación abrió {len(hits)} conexión(es) de red: {hits}"
    finally:
        client.stop()
        srv.close()


def test_a_model_that_is_not_downloaded_is_a_clear_error_and_only_a_download_mode_process_can_fetch(worker, qapp, tmp_path):
    client, done = worker
    original, _ = sloppy_ttml("es", "Baila_-_Alfonso_Lugo", 300)
    kw = dict(audio=str(DATA / "es" / "mp3" / "Baila_-_Alfonso_Lugo.mp3"), ffmpeg=find_ffmpeg(), cache_dir=str(tmp_path / "cache"), lang="es",
              lines=request_lines(from_ttml(original, 0)))
    ask(client, done, "align", model="no-existe/modelo-inventado", **kw)
    assert "no está descargado" in done["failed"][0] and "Traceback" not in done["failed"][0]
    ask(client, done, "fetch_model", lang="es")  # el proceso normal trabaja sin red: se niega a descargar
    assert "trabaja sin red" in done["failed"][0]

    dl, dl_done = SidecarClient(), {}
    dl.ready.connect(lambda: dl_done.update(ready=True))
    dl.result.connect(lambda rid, data: dl_done.update(result=data))
    dl.failed.connect(lambda rid, msg, detail: dl_done.update(failed=(msg, detail)))
    dl.start(worker_argv(str(env.env_python(LAB))), extra_env={"HF_HOME": str(LAB / "hf"), "HF_HUB_OFFLINE": "0"})  # modo descarga, explícito
    try:
        assert until(lambda: "ready" in dl_done, 30000)
        ask(dl, dl_done, "fetch_model", lang="es")  # ya estaba descargado: comprueba la ruta y no baja nada
        assert "failed" not in dl_done, dl_done.get("failed")
        assert Path(dl_done["result"]["path"]).exists() and dl_done["result"]["model"].endswith("spanish")
    finally:
        dl.stop()


def test_the_worker_loads_the_optional_separation_library_without_hanging(worker):
    """Regresión del cuelgue: con un hilo esperando órdenes en el pipe de stdin, importar audio-separator (SciPy, DLL nativas) se quedaba
    colgado para siempre. `probe` lo importa dentro del worker."""
    client, done = worker
    ask(client, done, "probe", timeout=90000)
    assert "failed" not in done, done.get("failed")
    assert done["result"]["separator"] is True and done["result"]["device"] == "cuda"


def test_a_missing_separation_model_is_a_clear_error_and_never_downloads(worker, tmp_path):
    client, done = worker
    original, _ = sloppy_ttml("es", "Baila_-_Alfonso_Lugo", 300)
    ask(client, done, "align", audio=str(DATA / "es" / "mp3" / "Baila_-_Alfonso_Lugo.mp3"), ffmpeg=find_ffmpeg(), cache_dir=str(tmp_path / "cache"),
        lang="es", lines=request_lines(from_ttml(original, 0)), separate="modelo_inexistente.ckpt")
    assert "no está descargado" in done["failed"][0] and "separación" in done["failed"][0]
    assert not (tmp_path / "separator-models").exists()  # no creó carpetas ni bajó nada


# ---------------- Modo B con el worker real ----------------

def baila_text():
    return [l for l in (DATA / "es" / "lyrics" / "Baila_-_Alfonso_Lugo.txt").read_text(encoding="utf-8").splitlines() if l.strip()]


def baila_truth():
    import csv
    return [round(float(r["word_start"]) * 1000) for r in csv.DictReader(open(DATA / "es" / "annotations" / "words" / "Baila_-_Alfonso_Lugo.csv", encoding="utf-8"))]


def test_mode_b_text_alignment_splits_spanish_into_syllables_and_exports_a_valid_ttml(worker, tmp_path):
    from marea.lab.align_job import from_text_result, text_request_lines
    from marea.lab.alignment import validate
    from marea.lab.ttml_out import export_ttml

    client, done = worker
    ask(client, done, "align", audio=str(DATA / "es" / "mp3" / "Baila_-_Alfonso_Lugo.mp3"), ffmpeg=find_ffmpeg(), cache_dir=str(tmp_path / "cache"),
        lang="es", lines=text_request_lines(baila_text()))
    assert "failed" not in done, done.get("failed")
    al = from_text_result(done["result"])
    words = [w for l in al.lines for w in l.words]
    assert any(len(w.syllables) >= 2 for w in words)  # silabeador corrió dentro del worker real
    assert all("".join(s.text for s in w.syllables) == w.text for w in words if w.syllables)  # y nunca cambió el texto
    assert validate(al) == []
    parsed = parse_ttml(export_ttml(al))
    assert len(parsed.lines) == len(al.lines) and [l.text for l in parsed.lines] == [l.text for l in al.lines]
    truth = baila_truth()
    starts = [w.start for w in words]
    assert len(starts) == len(truth) and sum(abs(a - b) <= 100 for a, b in zip(starts, truth)) / len(truth) >= 0.85


def test_windowed_realign_with_the_real_worker_agrees_with_the_full_song_alignment(worker, tmp_path):
    from marea.lab.align_job import text_request_lines
    client, done = worker
    kw = dict(audio=str(DATA / "es" / "mp3" / "Baila_-_Alfonso_Lugo.mp3"), ffmpeg=find_ffmpeg(), cache_dir=str(tmp_path / "cache"), lang="es")
    lines = text_request_lines(baila_text())
    ask(client, done, "align", lines=lines, **kw)
    full = {l["id"]: l for l in done["result"]["lines"]}["L6"]
    window = [max(0, full["start"] - 1500), full["end"] + 1500]
    ask(client, done, "align", lines=[lines[6]], window=window, **kw)
    part = done["result"]["lines"][0]
    assert window[0] <= part["start"] and part["end"] <= window[1]  # los tiempos vienen en el reloj de la canción, dentro del tramo
    assert abs(part["start"] - full["start"]) <= 150
    ask(client, done, "align", lines=[lines[6]], window=[5, 2], **kw)
    assert "no es válido" in done["failed"][0]


def test_whisper_draft_for_a_real_song_detects_the_language_and_is_mostly_right(worker, tmp_path):
    import re
    client, done = worker
    ask(client, done, "asr", timeout=300000, audio=str(DATA / "es" / "mp3" / "Baila_-_Alfonso_Lugo.mp3"), ffmpeg=find_ffmpeg(),
        cache_dir=str(tmp_path / "cache"), model=env.ASR_MODEL, lang="")
    if "failed" in done and "no está descargado" in done["failed"][0]:
        pytest.skip("Falta descargar el modelo de transcripción (fetch_asr).")
    data = done["result"]
    norm = lambda t: re.sub(r"[^\w\s]|_", " ", t.lower()).split()  # noqa: E731
    ref, hyp = norm(" ".join(baila_text())), norm(" ".join(l["text"] for l in data["lines"]))
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
    assert data["language"] == "es" and len(data["lines"]) > 20 and d[len(hyp)] / len(ref) < 0.45


# ---------------- la ventana de revisión con el Runner y el worker reales ----------------

def test_review_window_end_to_end_with_the_real_runner_and_worker(make_window, tmp_path, qapp):
    """Lo único que los tests con simulados no cubren: Runner + SidecarClient + worker real detrás de los botones de la ventana."""
    import hashlib

    from marea.project import project_from_ttml
    from marea.ui import lab
    from tests.test_lab_ui import FakePlayer

    stem = "Baila_-_Alfonso_Lugo"
    original, truth = sloppy_ttml("es", stem, 300)
    ttml = tmp_path / f"{stem}.ttml"
    ttml.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    win = make_window()
    project = project_from_ttml(ttml)
    project.audio_file = DATA / "es" / "mp3" / f"{stem}.mp3"  # sin audio_samples: la ventana lo decodifica sola (waveform y reproducción)
    win.load_project(project)
    win.settings.setValue(lab.LAB_DIR_KEY, str(LAB))
    rw = lab.LabReviewWindow(win.editor, player=FakePlayer())  # Runner real
    rw.show()
    try:
        rw.cb_lang.setCurrentIndex(rw.cb_lang.findData("es"))
        rw.cb_sep.setChecked(False)  # sin separar: el test dura segundos
        rw.btn_align.click()
        assert until(lambda: rw.pages.currentIndex() == 1, 120000), rw.lb_progress.text()
        assert rw.session is not None and rw.review_list.count() > 0 and "Alineado" in rw.lb_progress.text()
        assert until(lambda: rw._samples is not None, 60000)  # decodificó el audio para la forma de onda
        assert len(rw.timeline._peaks) > 1000
        rw.btn_approve.click()
        out = tmp_path / f"{stem}.aligned.ttml"
        assert out.exists() and hashlib.sha256(ttml.read_bytes()).hexdigest() == digest
        pred = [(s.begin, s.end) for l in parse_ttml(out.read_bytes()).lines for s in l.syllables]
        from marea.lab.metrics import word_errors
        assert word_errors(pred, truth)["start"]["within100"] >= 0.85
    finally:
        rw.close()
