"""Suite de aceptación para el Motor de Fondo Cinemático de ZH3 Marea."""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import skia
from PySide6.QtCore import QEventLoop, QTimer

from marea.audio import local
from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.engine.engine import Params
from marea.export import ffmpeg
from marea.export.job import PRESETS, ExportJob, clip_range, render_frames, run_export
from marea.export.queue import DONE, AudioSpec, QueueItem
from marea.model import cinematic as cine_model
from marea.model.cinematic import (
    CinematicConfig,
    CinematicInterval,
    CinematicTreatment,
    VideoResource,
    analyze_audio,
    compute_opening_trim_ms,
    evaluate_at,
    suggest_intervals,
)
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.project import ProjectError, demo_project, load_project_file, save_project
from marea.render.cinematic import CinematicBackground, VideoError, probe_video
from marea.render.scene import Scene, Style
from marea.ui.preview import PreviewWidget

SIZE = (270, 480)
STYLE = Style(title="Marea de luz", artist="Demo")


# ---------------- utilidades y generadores deterministas ----------------

def pump(ms: int = 20):
    """Bucle de eventos real para dejar trabajar hilos con subprocess."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def until(cond, timeout_ms: int = 12_000) -> bool:
    end = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < end:
        pump(20)
        if cond():
            return True
    return False


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(
        [
            ffmpeg.find_ffmpeg("ffprobe"), "-v", "error", "-count_frames",
            "-show_streams", "-show_format", "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def make_video(
    path: Path,
    frames_rgba: np.ndarray,
    fps: str = "30",
    audio_hz: float | None = None,
    sar: str | None = None,
) -> Path:
    """Codifica un array RGBA `(N, H, W, 4)` a MP4 H.264 determinista (con pista de audio opcional)."""
    n, h, w, c = frames_rgba.shape
    assert c == 4 and frames_rgba.dtype == np.uint8
    fps_val = (float(fps.split("/")[0]) / float(fps.split("/")[1])) if "/" in fps else float(fps)
    dur = n / fps_val

    cmd = [
        ffmpeg.find_ffmpeg("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", fps, "-i", "-",
    ]
    if audio_hz is not None:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={audio_hz}:sample_rate=44100:duration={dur:.4f}"]
    if sar:
        cmd += ["-vf", f"setsar={sar.replace(':', '/')}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p"]
    if audio_hz is not None:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, input=frames_rgba.tobytes(), check=True)
    return path


def warm_quadrant_frames(n: int = 60, w: int = 320, h: int = 180) -> np.ndarray:
    """Video cálido a pantalla completa con esquinas cromáticas distintas a la portada azul demo."""
    arr = np.zeros((n, h, w, 4), dtype=np.uint8)
    arr[..., 3] = 255
    for i in range(n):
        shift = (i * 3) % 45
        arr[i, : h // 2, : w // 2, :3] = (235, 110 + shift, 35)     # sup-izq: ámbar intenso
        arr[i, : h // 2, w // 2 :, :3] = (225, 55, 130 + shift)     # sup-der: magenta cálido
        arr[i, h // 2 :, : w // 2, :3] = (210, 175, 45 + shift)     # inf-izq: ocre dorado
        arr[i, h // 2 :, w // 2 :, :3] = (240, 85 + shift, 70)      # inf-der: coral
    return arr


def tri_segment_frames(fps: int = 30) -> np.ndarray:
    """3 segundos (90 frames): [0..1s] rojo, [1..2s] verde, [2..3s] amarillo."""
    arr = np.zeros((fps * 3, 160, 120, 4), dtype=np.uint8)
    arr[..., 3] = 255
    arr[:fps, ..., :3] = (235, 30, 35)
    arr[fps : 2 * fps, ..., :3] = (25, 225, 55)
    arr[2 * fps :, ..., :3] = (240, 215, 30)
    return arr


def checkerboard_frames(n: int = 30, w: int = 240, h: int = 320, cell: int = 16) -> np.ndarray:
    """Damero cromático de alto contraste para medir desenfoque, saturación, contraste y viñeta."""
    yy, xx = np.indices((h, w))
    mask = ((xx // cell) + (yy // cell)) % 2 == 0
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., 3] = 255
    frame[mask, :3] = (240, 75, 40)
    frame[~mask, :3] = (40, 195, 230)
    return np.broadcast_to(frame, (n, h, w, 4)).copy()


def stepped_60fps_frames(n: int = 90, w: int = 180, h: int = 320) -> np.ndarray:
    """Cada frame a 60 fps codifica su índice de forma monótona y visible."""
    arr = np.zeros((n, h, w, 4), dtype=np.uint8)
    arr[..., 3] = 255
    for i in range(n):
        arr[i, ..., 0] = min(250, 30 + (i * 5) % 220)
        arr[i, ..., 1] = min(250, 230 - (i * 4) % 200)
        arr[i, ..., 2] = min(250, 60 + ((i * 9) % 180))
    return arr


def make_cfg(res, enabled: bool = True, default_treatment: CinematicTreatment | None = None) -> CinematicConfig:
    cfg = CinematicConfig(
        enabled=enabled,
        default_treatment=copy.copy(default_treatment) if default_treatment else CinematicTreatment(),
    )
    cfg.set_primary_video(res)
    return cfg


@pytest.fixture
def win(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w


# ---------------- 1. Importación y reemplazo no destructivo ----------------

def test_01_import_and_replace_video_model_and_ui(win, tmp_path):
    v1 = make_video(tmp_path / "memoria_1.mp4", warm_quadrant_frames(72, 320, 180), fps="24")
    v2 = make_video(tmp_path / "memoria_2.mp4", warm_quadrant_frames(45, 240, 320), fps="30")
    v1_bytes = v1.read_bytes()
    v1_mtime = v1.stat().st_mtime_ns

    e = win.editor
    assert e.btn_video.text() == "Importar video…"
    assert not e.cb_cinematic.isChecked()

    e.import_video(v1)
    assert until(lambda: not e._video_busy and e.project.cinematic.primary_video is not None)
    res1 = e.project.cinematic.primary_video
    assert (res1.width, res1.height) == (320, 180)
    assert res1.fps == pytest.approx(24.0, abs=0.05)
    assert res1.duration_ms == pytest.approx(3000.0, abs=120.0)
    assert e.cb_cinematic.isChecked() is True
    assert e.btn_video.text() == "Reemplazar video…"
    assert "memoria_1.mp4" in e.lb_video_info.text() and "320×180" in e.lb_video_info.text() and "24 fps" in e.lb_video_info.text()

    # Añadimos un momento que usa el tramo [1.0 s .. 2.8 s] del primer video (3.0 s)
    iv = e.project.cinematic.add_interval(8000.0, 11000.0, video_start_ms=1000.0, video_end_ms=2800.0)
    e._refresh_cinematic_tab(keep_id=iv.id)

    # Reemplazamos por un video más corto (1.5 s, 240×320, 30 fps)
    e.import_video(v2)
    assert until(lambda: not e._video_busy and Path(e.project.cinematic.primary_video.path) == v2.resolve())
    res2 = e.project.cinematic.primary_video
    assert (res2.width, res2.height) == (240, 320)
    assert res2.fps == pytest.approx(30.0, abs=0.05)
    assert res2.duration_ms == pytest.approx(1500.0, abs=120.0)
    assert "memoria_2.mp4" in e.lb_video_info.text() and "240×320" in e.lb_video_info.text()

    # El intervalo se conserva, apunta al nuevo video y su segmento se ajusta a la nueva duración
    assert len(e.project.cinematic.intervals) == 1
    kept = e.project.cinematic.intervals[0]
    assert kept.video_id == res2.id
    assert 0.0 <= kept.video_start_ms < kept.video_end_ms <= res2.duration_ms + 1.0

    # El archivo original jamás se modifica
    assert v1.read_bytes() == v1_bytes
    assert v1.stat().st_mtime_ns == v1_mtime


# ---------------- 2. Exclusión total del audio del video ----------------

def test_02_video_audio_is_excluded_from_lyrics_and_song_exports(win, tmp_path):
    # Video con pista de audio aguda de 880 Hz embebida
    vid = make_video(tmp_path / "con_audio_880.mp4", warm_quadrant_frames(60, 180, 320), fps="30", audio_hz=880.0)
    vid_probe = ffprobe_json(vid)
    assert sorted(s["codec_name"] for s in vid_probe["streams"]) == ["aac", "h264"]

    res = probe_video(vid)
    cfg = make_cfg(res, enabled=True)
    cfg.add_interval(12000.0, 15000.0, video_start_ms=0.0, video_end_ms=2000.0, fade_in_ms=200.0, fade_out_ms=200.0)

    cover_bytes = bytes(demo_cover().encodeToData())
    out_lyrics = tmp_path / "solo_letra.mp4"
    job_lyrics = ExportJob(
        exact_lyrics(), cover_bytes, out_lyrics, 12500.0, 13500.0,
        size=(180, 320), fps=30, prefer_gpu=False, style=STYLE, cinematic=cfg,
    )
    run_export(job_lyrics)
    info_lyrics = ffprobe_json(out_lyrics)
    assert [s["codec_name"] for s in info_lyrics["streams"]] == ["h264"]  # 0 pistas de audio

    # Export «Con canción» a través de ExportQueue con pista musical de 220 Hz
    t = np.arange(local.RATE * 20, dtype=np.float32) / local.RATE
    song_220 = (0.55 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)
    stereo_220 = np.column_stack([song_220, song_220])

    out_song = tmp_path / "con_cancion.mp4"
    job_song = ExportJob(
        exact_lyrics(), cover_bytes, out_song, 12500.0, 13500.0,
        size=(180, 320), fps=30, prefer_gpu=False, style=STYLE, cinematic=copy.deepcopy(cfg),
    )
    audio_spec = AudioSpec("local", 12500.0, 13500.0, samples=stereo_220, offset_ms=0.0, fade_out_ms=0.0)
    item = QueueItem("Marea de luz", "Demo", cover_bytes, job_song, audio_spec)
    win.queue.add(item)
    assert until(lambda: item.state not in ("queued", "audio", "render"), timeout_ms=45_000)
    assert item.state == DONE, item.error

    info_song = ffprobe_json(out_song)
    assert sorted(s["codec_name"] for s in info_song["streams"]) == ["aac", "h264"]

    exported_audio = local.decode(out_song)[:, 0]
    spec = np.abs(np.fft.rfft(exported_audio))
    freqs = np.fft.rfftfreq(len(exported_audio), d=1.0 / local.RATE)
    mag_220 = float(spec[(freqs > 205) & (freqs < 235)].max())
    mag_880 = float(spec[(freqs > 865) & (freqs < 895)].max())
    assert mag_220 > 100.0
    assert mag_880 < mag_220 * 0.005  # el audio de 880 Hz del video jamás entra en la mezcla


# ---------------- 3. Alternancia portada <-> video a pantalla completa ----------------

def test_03_alternates_between_album_cover_and_fullscreen_video(tmp_path):
    vid = make_video(tmp_path / "full.mp4", warm_quadrant_frames(60, 320, 180), fps="30")
    res = probe_video(vid)
    tr = CinematicTreatment(opacity=1.0, blur=0.15, dim=0.10, vignette=0.15, grain=0.0, saturation=1.0, contrast=1.0)
    cfg = make_cfg(res, enabled=True, default_treatment=tr)
    cfg.add_interval(12000.0, 16000.0, video_start_ms=0.0, video_end_ms=2000.0, fade_in_ms=600.0, fade_out_ms=600.0, treatment=tr)

    base_scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE)
    cine_scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE, cinematic=cfg, async_video=False)
    try:
        # Antes del intervalo: idéntico bit a bit al fondo de portada animada
        b_before = base_scene.snapshot(11000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        c_before = cine_scene.snapshot(11000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        assert np.array_equal(c_before, b_before)

        # Durante el intervalo: opaco, cubre todo el lienzo (las 4 esquinas y >90 % de píxeles cambian; no es ventana PiP)
        b_mid = base_scene.snapshot(14000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        c_mid = cine_scene.snapshot(14000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        assert c_mid[..., 3].min() == 255
        delta = np.abs(c_mid[..., :3].astype(int) - b_mid[..., :3].astype(int)).sum(axis=2)
        assert float((delta > 25).mean()) > 0.90
        for y, x in ((2, 2), (2, SIZE[0] - 3), (SIZE[1] - 3, 2), (SIZE[1] - 3, SIZE[0] - 3)):
            assert delta[y, x] > 40, (y, x, delta[y, x])

        # Después del intervalo: retorno limpio e idéntico bit a bit a la portada animada
        b_after = base_scene.snapshot(17000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        c_after = cine_scene.snapshot(17000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        assert np.array_equal(c_after, b_after)
    finally:
        cine_scene.close()


# ---------------- 4. Creación, edición y borrado manual + mapeo determinista ----------------

def test_04_manual_intervals_crud_and_deterministic_timeline_mapping(win, tmp_path):
    # Video de 3 s (rojo [0..1s], verde [1..2s], amarillo [2..3s]) frente a canción demo de ~48 s
    vid = make_video(tmp_path / "segmentos.mp4", tri_segment_frames(30), fps="30")
    e = win.editor
    e.import_video(vid)
    assert until(lambda: not e._video_busy and e.project.cinematic.primary_video is not None)

    # Crear intervalo manual desde la UI
    e._add_cinematic_interval()
    assert e.iv_list.count() == 1

    # Editar controles: música [20.0s .. 22.0s] mapeada al segmento rojo [0.15s .. 0.85s], sin velo ni tinte alterado
    tr = CinematicTreatment(opacity=1.0, blur=0.0, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0)
    e.sl_cine_op.setValue(100)
    e.sl_cine_blur.setValue(0)
    e.sl_cine_dim.setValue(0)
    e.sl_cine_vig.setValue(0)
    e.sl_cine_grain.setValue(0)
    e.sl_cine_sat.setValue(100)
    e.sl_cine_con.setValue(100)

    e.sp_iv_start.setValue(20.0)
    e.sp_iv_end.setValue(22.0)
    e.sp_vid_start.setValue(0.15)
    e.sp_vid_end.setValue(0.85)
    e.sp_fade_in.setValue(0.2)
    e.sp_fade_out.setValue(0.2)

    iv = e.project.cinematic.intervals[0]
    assert (iv.start_ms, iv.end_ms) == (20000.0, 22000.0)
    assert (iv.video_start_ms, iv.video_end_ms) == (150.0, 850.0)
    act_red = evaluate_at(e.project.cinematic, 21000.0)
    assert len(act_red) == 1 and act_red[0].src_ms == pytest.approx(500.0, abs=5.0)

    no_fg = Style(title="", artist="", wm_text="", bloom=False, credit=False)
    empty_ly = Lyrics([Line(0, 100, syllables=[Syllable("x", 0, 100, True)])])
    s_red = Scene(empty_ly, demo_cover(), (120, 160), style=no_fg, cinematic=copy.deepcopy(e.project.cinematic), async_video=False)
    try:
        px_red = s_red.snapshot(21000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        mean_rgb_red = px_red[..., :3].mean(axis=(0, 1))
        assert mean_rgb_red[0] > 180 and mean_rgb_red[0] > mean_rgb_red[1] + 100
    finally:
        s_red.close()

    # Cambiar el segmento de video del MISMO instante musical [20..22s] al tramo verde [1.15s .. 1.85s]
    e.sp_vid_start.setValue(1.15)
    e.sp_vid_end.setValue(1.85)
    act_green = evaluate_at(e.project.cinematic, 21000.0)
    assert len(act_green) == 1 and act_green[0].src_ms == pytest.approx(1500.0, abs=5.0)

    s_green = Scene(empty_ly, demo_cover(), (120, 160), style=no_fg, cinematic=copy.deepcopy(e.project.cinematic), async_video=False)
    try:
        px_green = s_green.snapshot(21000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        mean_rgb_green = px_green[..., :3].mean(axis=(0, 1))
        assert mean_rgb_green[1] > 170 and mean_rgb_green[1] > mean_rgb_green[0] + 100
    finally:
        s_green.close()

    # Eliminar el intervalo manual
    e._remove_cinematic_interval()
    assert e.iv_list.count() == 0 and len(e.project.cinematic.intervals) == 0
    assert evaluate_at(e.project.cinematic, 21000.0) == []
    assert tr.opacity == 1.0


# ---------------- 5. Sugerencia automática de momentos y caché de análisis ----------------

def test_05_automatic_moment_suggestions_and_cached_analysis(win, tmp_path):
    sr = local.RATE
    dur_s = 36
    t = np.arange(sr * dur_s, dtype=np.float32) / sr
    # Audio con estructura musical real: intro suave, pausa en 10-12 s, subida/acento en 12.2 s, respiro en 22-24 s y clímax en 24.5 s
    env = np.full_like(t, 0.12)
    env[(t >= 5.0) & (t < 10.0)] = 0.30
    env[(t >= 10.0) & (t < 12.0)] = 0.01   # pausa
    env[(t >= 12.0) & (t < 18.0)] = 0.75   # entrada intensa tras pausa
    env[(t >= 22.0) & (t < 24.2)] = 0.015  # segunda pausa
    env[(t >= 24.2) & (t < 31.0)] = 0.85   # segundo cambio de intensidad
    carrier = np.sin(2 * np.pi * 220 * t) + 0.4 * np.sin(2 * np.pi * 440 * t)
    dynamic_audio = (env * carrier * 0.5).astype(np.float32)

    cine_model._ANALYSIS_CACHE.clear()
    c0 = cine_model._analysis_computations
    ivs_1 = suggest_intervals(
        exact_lyrics(),
        video_duration_ms=7000.0,  # duración distinta a la canción
        audio_samples=dynamic_audio,
        sample_rate=sr,
        range_ms=(0.0, 36000.0),
    )
    c1 = cine_model._analysis_computations
    assert c1 == c0 + 1
    assert len(ivs_1) >= 2

    # Segunda llamada con el mismo audio: reutiliza análisis en caché sin recalcular
    ivs_2 = suggest_intervals(
        exact_lyrics(),
        video_duration_ms=7000.0,
        audio_samples=dynamic_audio,
        sample_rate=sr,
        range_ms=(0.0, 36000.0),
    )
    c2 = cine_model._analysis_computations
    assert c2 == c1
    assert [(round(x.start_ms), round(x.end_ms)) for x in ivs_1] == [(round(x.start_ms), round(x.end_ms)) for x in ivs_2]
    assert analyze_audio(dynamic_audio, sr) is analyze_audio(dynamic_audio, sr)
    assert cine_model._analysis_computations == c1

    # Respeta límites de cobertura, huecos de alternancia y duración del video fuente
    covered = sum(iv.duration_ms for iv in ivs_1)
    assert covered <= 36000.0 * 0.46
    for a, b in zip(ivs_1, ivs_1[1:]):
        assert b.start_ms - a.end_ms >= cine_model.MIN_GAP_MS - 1.0
    for iv in ivs_1:
        assert 0.0 <= iv.video_start_ms < iv.video_end_ms <= 7000.0

    # Anti-umbral: un tono fuerte y constante (volumen alto todo el tiempo) sin interludios ni acentos NO activa todo el clip
    flat_lines = [
        Line(
            i * 2200 + 200,
            i * 2200 + 2000,
            syllables=[
                Syllable("ver", i * 2200 + 200, i * 2200 + 700, False),
                Syllable("so", i * 2200 + 700, i * 2200 + 1100, True),
                Syllable("pla", i * 2200 + 1120, i * 2200 + 1550, False),
                Syllable("no", i * 2200 + 1550, i * 2200 + 2000, True),
            ],
        )
        for i in range(15)
    ]
    flat_lyrics = Lyrics(lines=flat_lines)
    assert flat_lyrics.interludes == []
    loud_constant = (0.85 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    flat_ivs = suggest_intervals(flat_lyrics, 10_000.0, audio_samples=loud_constant, sample_rate=sr, range_ms=(0.0, 30_000.0))
    assert flat_ivs == []  # volumen alto constante != momento cinemático

    # Integración con el botón «Sugerir momentos» del Editor (los momentos quedan editables)
    vid = make_video(tmp_path / "sugerir.mp4", warm_quadrant_frames(90, 240, 320), fps="30")
    e = win.editor
    e.import_video(vid)
    assert until(lambda: not e._video_busy and e.project.cinematic.primary_video is not None)
    e._suggest_cinematic()
    assert e.iv_list.count() >= 1
    e.iv_list.setCurrentRow(0)
    old_end = e.project.cinematic.intervals[0].end_ms
    e.sp_iv_end.setValue(old_end / 1000.0 + 0.5)
    assert e.project.cinematic.intervals[0].end_ms == pytest.approx(old_end + 500.0, abs=5.0)


# ---------------- 6. Transiciones suaves y estética de memoria ----------------

def test_06_transitions_and_memory_aesthetic_effects(tmp_path):
    vid = make_video(tmp_path / "estetica.mp4", checkerboard_frames(45, 240, 320, cell=16), fps="30")
    res = probe_video(vid)

    def render_bg(treatment: CinematicTreatment, t_ms: float = 5000.0, lyric_act: float = 0.0,
                  fade_in: float = 1000.0, fade_out: float = 1000.0) -> np.ndarray:
        cfg = make_cfg(res, enabled=True, default_treatment=treatment)
        cfg.add_interval(3000.0, 8000.0, video_start_ms=0.0, video_end_ms=1500.0,
                         fade_in_ms=fade_in, fade_out_ms=fade_out, treatment=treatment)
        scene = Scene(exact_lyrics(), demo_cover(), (240, 320), style=Style(title="", artist="", wm_text=""),
                      cinematic=cfg, async_video=False)
        try:
            surf = skia.Surface(240, 320)
            c = surf.getCanvas()
            assert scene.cinematic_bg is not None
            scene.cinematic_bg.draw(c, scene.bg, t_ms / 1000.0, lyric_act=lyric_act)
            return surf.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)
        finally:
            scene.close()

    base_scene = Scene(exact_lyrics(), demo_cover(), (240, 320))
    def render_cover(t_ms: float) -> np.ndarray:
        surf = skia.Surface(240, 320)
        base_scene.bg.draw(surf.getCanvas(), t_ms / 1000.0)
        return surf.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)

    neutral = CinematicTreatment(opacity=1.0, blur=0.0, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0)

    # 6a. Fade-in y fade-out monótonos y suaves
    in_weights = []
    for t_ms in (3000.0, 3250.0, 3500.0, 3750.0, 4000.0):
        px = render_bg(neutral, t_ms=t_ms, fade_in=1000.0)
        cov = render_cover(t_ms)
        in_weights.append(float(np.abs(px[..., :3].astype(float) - cov[..., :3].astype(float)).mean()))
    assert in_weights[0] == pytest.approx(0.0, abs=0.5)
    assert all(a < b for a, b in zip(in_weights, in_weights[1:])), in_weights

    out_weights = []
    for t_ms in (7000.0, 7250.0, 7500.0, 7750.0, 8000.0):
        px = render_bg(neutral, t_ms=t_ms, fade_out=1000.0)
        cov = render_cover(t_ms)
        out_weights.append(float(np.abs(px[..., :3].astype(float) - cov[..., :3].astype(float)).mean()))
    assert out_weights[-1] == pytest.approx(0.0, abs=0.5)
    assert all(a > b for a, b in zip(out_weights, out_weights[1:])), out_weights

    # 6b. Desenfoque suave + dinámico con actividad vocal, sin bordes transparentes
    sharp = render_bg(neutral)
    soft = render_bg(CinematicTreatment(opacity=1.0, blur=0.80, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0))
    lyric_soft = render_bg(neutral, lyric_act=1.0)
    grad = lambda im: float(np.abs(np.diff(im[..., :3].astype(float), axis=1)).mean() + np.abs(np.diff(im[..., :3].astype(float), axis=0)).mean())
    assert grad(soft) < grad(sharp) * 0.45
    assert grad(lyric_soft) < grad(sharp) * 0.90  # la voz activa añade desenfoque dinámico sutil
    assert soft[..., 3].min() == 255

    # 6c. Viñeta oscurece esquinas respecto al centro
    vig = render_bg(CinematicTreatment(opacity=1.0, blur=0.3, dim=0.0, vignette=0.95, grain=0.0, saturation=1.0, contrast=1.0))
    no_vig = render_bg(CinematicTreatment(opacity=1.0, blur=0.3, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0))
    corner_ratio_vig = vig[:28, :28, :3].mean() / max(1.0, vig[130:190, 90:150, :3].mean())
    corner_ratio_novig = no_vig[:28, :28, :3].mean() / max(1.0, no_vig[130:190, 90:150, :3].mean())
    assert corner_ratio_vig < corner_ratio_novig * 0.72

    # 6d. Velo (dim) reduce luminancia para priorizar legibilidad tipográfica
    dimmed = render_bg(CinematicTreatment(opacity=1.0, blur=0.2, dim=0.55, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0))
    undimmed = render_bg(CinematicTreatment(opacity=1.0, blur=0.2, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0))
    assert dimmed[..., :3].mean() < undimmed[..., :3].mean() * 0.60

    # 6e. Opacidad gradúa la mezcla con la portada
    half_op = render_bg(CinematicTreatment(opacity=0.45, blur=0.0, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0))
    cov_5s = render_cover(5000.0)
    dist_half = np.abs(half_op[..., :3].astype(float) - cov_5s[..., :3].astype(float)).mean()
    dist_full = np.abs(sharp[..., :3].astype(float) - cov_5s[..., :3].astype(float)).mean()
    assert 0.30 * dist_full < dist_half < 0.65 * dist_full

    # 6f. Grano fílmico determinista: mismo instante => mismos píxeles; distinto cuadro de 24 Hz => grano distinto
    tr_grain = CinematicTreatment(opacity=1.0, blur=0.5, dim=0.1, vignette=0.0, grain=0.45, saturation=1.0, contrast=1.0)
    tr_nograin = CinematicTreatment(opacity=1.0, blur=0.5, dim=0.1, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0)
    g1 = render_bg(tr_grain, t_ms=5000.0)
    g2 = render_bg(tr_grain, t_ms=5000.0)
    g_next = render_bg(tr_grain, t_ms=5060.0)
    g0 = render_bg(tr_nograin, t_ms=5000.0)
    assert np.array_equal(g1, g2)
    assert not np.array_equal(g1, g0)
    assert not np.array_equal(g1, g_next)

    # 6g. Saturación y contraste
    desat = render_bg(CinematicTreatment(opacity=1.0, blur=0.0, dim=0.0, vignette=0.0, grain=0.0, saturation=0.0, contrast=1.0))
    chroma_span = (desat[..., :3].max(axis=2).astype(int) - desat[..., :3].min(axis=2).astype(int)).mean()
    assert chroma_span < 3.0  # gris neutro
    low_con = render_bg(CinematicTreatment(opacity=1.0, blur=0.0, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=0.65))
    hi_con = render_bg(CinematicTreatment(opacity=1.0, blur=0.0, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.35))
    assert hi_con[..., :3].astype(float).std() > low_con[..., :3].astype(float).std() * 1.35


# ---------------- 7. Letras y elementos de primer plano intactos ----------------

def test_07_foreground_lyrics_header_interlude_credits_and_watermark_intact(tmp_path):
    vid = make_video(tmp_path / "fg.mp4", warm_quadrant_frames(60, 240, 320), fps="30")
    res = probe_video(vid)

    # Con Cinemático desactivado (aunque haya video e intervalos cargados), el frame es 100 % idéntico al motor original
    cfg_off = make_cfg(res, enabled=False)
    cfg_off.add_interval(0.0, 50_000.0, video_start_ms=0.0, video_end_ms=2000.0)
    s_orig = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE)
    s_off = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE, cinematic=cfg_off, async_video=False)
    try:
        assert np.array_equal(
            s_orig.snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType),
            s_off.snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType),
        )
    finally:
        s_off.close()

    # Con Cinemático activo, letras activas + bloom + cabecera + marca de agua + créditos siguen en primer plano
    cfg_on = make_cfg(res, enabled=True)
    cfg_on.add_interval(0.0, 60_000.0, video_start_ms=0.0, video_end_ms=2000.0, fade_in_ms=100.0, fade_out_ms=100.0)

    ly = exact_lyrics()
    ly.credit, ly.writers = "Apple Music", ["Ana Pérez", "Beto Ruiz"]
    st_full = Style(title="Marea de luz", artist="Demo", bloom=True, credit=True, wm_text="ZH3 Marea")
    st_nobloom = Style(title="Marea de luz", artist="Demo", bloom=False, credit=True, wm_text="ZH3 Marea")
    st_bare = Style(title="", artist="", bloom=True, credit=False, wm_text="")

    s_full = Scene(ly, demo_cover(), SIZE, style=st_full, cinematic=cfg_on, async_video=False)
    s_nobloom = Scene(ly, demo_cover(), SIZE, style=st_nobloom, cinematic=cfg_on, async_video=False)
    s_bare = Scene(ly, demo_cover(), SIZE, style=st_bare, cinematic=cfg_on, async_video=False)
    try:
        px_full = s_full.snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
        px_nobloom = s_nobloom.snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
        px_bare = s_bare.snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)

        # La línea activa cantando brilla en blanco nítido sobre el fondo cinemático
        assert (px_full[..., :3].sum(axis=2) > 680).sum() > 200
        # El bloom sigue sumando luz sobre el video
        assert (px_full[..., :3] - px_nobloom[..., :3]).sum() > 0
        # Cabecera (arriba) y marca de agua (WM_BASELINE = 0.835) siguen presentes frente a la escena sin cabecera/watermark
        diff_chrome = np.abs(px_full[..., :3] - px_bare[..., :3]).sum(axis=2)
        assert (diff_chrome[: int(SIZE[1] * 0.18)] > 25).sum() > 80   # cabecera
        assert (diff_chrome[int(SIZE[1] * 0.78) : int(SIZE[1] * 0.86)] > 15).sum() > 20   # marca de agua

        # Puntos de interludio (t = 3000 ms, intro instrumental [0..6000 ms]) siguen presentes en primer plano
        s_interlude = Scene(ly, demo_cover(), SIZE, style=st_bare, cinematic=cfg_on, async_video=False)
        s_nolines = Scene(Lyrics([Line(50_000, 51_000, syllables=[Syllable("x", 50_000, 51_000, True)])]),
                          demo_cover(), SIZE, style=st_bare, cinematic=cfg_on, async_video=False)
        try:
            # Con 50_000 ms también habría intro; comparamos contra escena sin puntos (línea desde t=0)
            s_nodots = Scene(Lyrics([Line(0, 500, syllables=[Syllable(" ", 0, 500, True)])]),
                             demo_cover(), SIZE, style=st_bare, cinematic=cfg_on, async_video=False)
            try:
                px_dots = s_interlude.snapshot(3000.0).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
                px_nodots = s_nodots.snapshot(3000.0).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
                assert (np.abs(px_dots[..., :3] - px_nodots[..., :3]).sum(axis=2) > 25).sum() > 15
            finally:
                s_nodots.close()
        finally:
            s_interlude.close()
            s_nolines.close()

        # Créditos al final de la canción siguen dibujándose encima del fondo cinemático
        end_t = ly.lines[-1].end + 800.0
        px_cred = s_full.snapshot(end_t).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
        px_nocred = s_bare.snapshot(end_t).toarray(colorType=skia.kRGBA_8888_ColorType).astype(int)
        assert (np.abs(px_cred[..., :3] - px_nocred[..., :3]).sum(axis=2) > 30).sum() > 50
    finally:
        s_full.close()
        s_nobloom.close()
        s_bare.close()


# ---------------- 8. Sincronización determinista 60 FPS Preview <-> Export ----------------

def test_08_deterministic_60fps_sync_between_preview_and_export_and_nonblocking_ui(qapp, tmp_path):
    vid = make_video(tmp_path / "sync60.mp4", stepped_60fps_frames(90, 180, 320), fps="60")
    res = probe_video(vid)
    cfg = make_cfg(res, enabled=True)
    cfg.add_interval(12000.0, 15000.0, video_start_ms=0.0, video_end_ms=1500.0, fade_in_ms=250.0, fade_out_ms=250.0)

    proj = demo_project()
    # Alinear exactamente la imagen de portada con el PNG codificado que recibe ExportJob
    cover_bytes = proj.cover_png()
    proj.cover = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(cover_bytes))
    proj.cinematic = copy.deepcopy(cfg)

    start_ms = 12500.0
    n_frames = 15
    end_ms = start_ms + n_frames / 60.0 * 1000.0
    st = Style(title=proj.title, artist=proj.artist)
    pa = Params()

    job = ExportJob(
        proj.lyrics, cover_bytes, tmp_path / "sync.mp4", start_ms, end_ms,
        size=SIZE, fps=60, prefer_gpu=False, params=replace_params(pa), style=Style(**vars(st)),
        cinematic=copy.deepcopy(cfg),
    )
    export_run_1 = list(render_frames(job))
    export_run_2 = list(render_frames(job))
    dig_1 = [hashlib.sha256(f.tobytes()).hexdigest() for f in export_run_1]
    dig_2 = [hashlib.sha256(f.tobytes()).hexdigest() for f in export_run_2]
    assert dig_1 == dig_2 and len(dig_1) == n_frames
    assert len(set(dig_1)) == n_frames  # cada paso de 1/60 s produce su frame exacto

    # PreviewWidget con decodificación asíncrona (no bloquea el hilo principal de Qt)
    pw = PreviewWidget(prefer_gpu=False, async_video=True)
    try:
        ready_signals = []
        pw._video_frame_ready.connect(lambda: ready_signals.append(1))
        t0 = time.perf_counter()
        pw.set_project(proj, st, pa, (start_ms, end_ms + 1000.0))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 400.0  # set_project + warmup no bloquea el hilo principal decodificando video
        assert until(lambda: len(ready_signals) >= 1, timeout_ms=5000)

        # Paridad determinista cuadro a cuadro a 1/60 s exactos (deteniendo el timer de settle en pausa)
        pw._timer.stop()
        pw.scene.engine.warmup(start_ms)
        pw._render()
        pw.wait_video()
        preview_frames = [pw._frame.copy()]
        for _ in range(1, n_frames):
            pw.scene.engine.advance(1.0 / 60.0, running=True)
            pw._render()
            pw.wait_video()
            preview_frames.append(pw._frame.copy())

        for idx, (pf, ef) in enumerate(zip(preview_frames, export_run_1)):
            assert np.array_equal(pf, ef), f"Desajuste Preview vs Export en frame {idx}"
    finally:
        pw.shutdown()


def replace_params(p: Params) -> Params:
    return copy.deepcopy(p)


# ---------------- 9. Distintas resoluciones, relaciones de aspecto, SAR y FPS ----------------

@pytest.mark.parametrize(
    "w,h,fps,sar,expected_fps",
    [
        (640, 360, "24", None, 24.0),
        (1280, 720, "30000/1001", None, 29.97),
        (360, 640, "60", None, 60.0),
        (320, 360, "25", "2:1", 25.0),
    ],
)
def test_09_multiple_resolutions_aspect_ratios_framerates_and_sar(tmp_path, w, h, fps, sar, expected_fps):
    n_frames = 36
    vid = make_video(tmp_path / f"v_{w}x{h}.mp4", warm_quadrant_frames(n_frames, w, h), fps=fps, sar=sar)
    res = probe_video(vid)
    assert (res.width, res.height) == (w, h)
    assert res.fps == pytest.approx(expected_fps, abs=0.05)
    if sar == "2:1":
        assert (res.sar_num, res.sar_den) == (2, 1)

    tr = CinematicTreatment(opacity=1.0, blur=0.1, dim=0.0, vignette=0.0, grain=0.0, saturation=1.0, contrast=1.0)
    cfg = make_cfg(res, enabled=True, default_treatment=tr)
    cfg.add_interval(10000.0, 14000.0, video_start_ms=0.0, video_end_ms=1000.0, fade_in_ms=100.0, fade_out_ms=100.0, treatment=tr)

    scene = Scene(exact_lyrics(), demo_cover(), SIZE, style=Style(title="", artist="", wm_text=""),
                  cinematic=cfg, async_video=False)
    try:
        px = scene.snapshot(12000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        assert px.shape == (SIZE[1], SIZE[0], 4)
        assert px[..., 3].min() == 255
        # Aspect-fill sin bandas negras ni letterbox/pillarbox en ningún borde
        assert px[0, :, :3].sum(axis=1).min() > 60
        assert px[-1, :, :3].sum(axis=1).min() > 60
        assert px[:, 0, :3].sum(axis=1).min() > 60
        assert px[:, -1, :3].sum(axis=1).min() > 60
    finally:
        scene.close()


# ---------------- 10. Persistencia: guardar y reabrir proyecto sin pérdida ----------------

def test_10_project_save_and_reopen_roundtrip_and_version_validation(make_window, qapp, tmp_path):
    vid = make_video(tmp_path / "persist.mp4", warm_quadrant_frames(60, 320, 180), fps="30")

    w1 = make_window("s1.ini")
    w1.show()
    w1.load_demo()
    e1 = w1.editor
    e1.import_video(vid)
    assert until(lambda: not e1._video_busy and e1.project.cinematic.primary_video is not None)

    e1.ed_title.setText("Memoria en el mar")
    e1.ed_artist.setText("ZH3")
    e1.first, e1.last = 1, 4
    e1._range_changed()
    e1.project.audio_offset_ms = -175.0
    e1.cb_bloom.setChecked(False)
    e1.cmb_motion.setCurrentIndex(e1.cmb_motion.findData("respira"))
    e1.sl_amt.setValue(65)

    custom_tr = CinematicTreatment(opacity=0.88, blur=0.42, dim=0.33, vignette=0.51, grain=0.14, saturation=0.84, contrast=1.08)
    e1.project.cinematic.default_treatment = custom_tr
    e1.project.cinematic.intervals.clear()
    e1.project.cinematic.add_interval(9000.0, 13000.0, video_start_ms=100.0, video_end_ms=1600.0,
                                      fade_in_ms=700.0, fade_out_ms=900.0, treatment=custom_tr)
    e1.project.cinematic.add_interval(18000.0, 22500.0, video_start_ms=300.0, video_end_ms=1900.0,
                                      fade_in_ms=850.0, fade_out_ms=950.0, treatment=custom_tr)
    e1._refresh_cinematic_tab()

    proj_file = tmp_path / "memoria.marea.json"
    save_project(e1.project, proj_file, editor_state=e1.editor_state())
    assert proj_file.is_file()

    s1 = Scene(e1.project.lyrics, e1.project.cover, SIZE, params=copy.deepcopy(e1.params),
               style=Style(**vars(e1.style)), cinematic=copy.deepcopy(e1.project.cinematic), async_video=False)
    snap_1 = s1.snapshot(11000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
    s1.close()
    w1.close()

    # Reabrir en una ventana completamente nueva
    w2 = make_window("s2.ini")
    w2.show()
    assert w2.open_project_file(proj_file) is True
    qapp.processEvents()
    e2 = w2.editor

    assert (e2.project.title, e2.project.artist) == ("Memoria en el mar", "ZH3")
    assert (e2.first, e2.last) == (1, 4)
    assert e2.project.audio_offset_ms == pytest.approx(-175.0)
    assert e2.style.bloom is False and e2.params.motion == "respira" and e2.params.motion_amt == pytest.approx(0.65)
    assert e2.cb_cinematic.isChecked() is True
    assert len(e2.project.cinematic.intervals) == 2
    iv0, iv1 = e2.project.cinematic.intervals
    assert (iv0.start_ms, iv0.end_ms, iv0.video_start_ms, iv0.video_end_ms) == (9000.0, 13000.0, 100.0, 1600.0)
    assert (iv1.fade_in_ms, iv1.fade_out_ms) == (850.0, 950.0)
    assert iv0.treatment.blur == pytest.approx(0.42) and iv0.treatment.vignette == pytest.approx(0.51)

    s2 = Scene(e2.project.lyrics, e2.project.cover, SIZE, params=copy.deepcopy(e2.params),
               style=Style(**vars(e2.style)), cinematic=copy.deepcopy(e2.project.cinematic), async_video=False)
    snap_2 = s2.snapshot(11000.0).toarray(colorType=skia.kRGBA_8888_ColorType)
    s2.close()
    assert np.array_equal(snap_1, snap_2)

    # Validar rechazo limpio de versiones futuras desconocidas
    bad_ver = tmp_path / "futuro.marea.json"
    raw = json.loads(proj_file.read_text(encoding="utf-8"))
    raw["version"] = 999
    bad_ver.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProjectError):
        load_project_file(bad_ver)
    assert w2.open_project_file(bad_ver) is False
    w2.close()


# ---------------- 11. Formatos de exportación: 1080×1920, 1080×1080, 1920×1080 ----------------

@pytest.mark.parametrize("preset_key,expected_size", [
    ("tiktok", (1080, 1920)),
    ("square", (1080, 1080)),
    ("wide", (1920, 1080)),
])
def test_11_all_supported_export_formats_1080x1920_1080x1080_1920x1080(tmp_path, preset_key, expected_size):
    _, w, h = PRESETS[preset_key]
    assert (w, h) == expected_size
    vid = make_video(tmp_path / f"src_{preset_key}.mp4", warm_quadrant_frames(30, 320, 240), fps="30")
    res = probe_video(vid)
    cfg = make_cfg(res, enabled=True)
    cfg.add_interval(12000.0, 15000.0, video_start_ms=0.0, video_end_ms=1000.0, fade_in_ms=100.0, fade_out_ms=100.0)

    out = tmp_path / f"out_{preset_key}.mp4"
    n_frames = 4
    job = ExportJob(
        exact_lyrics(), bytes(demo_cover().encodeToData()), out,
        12500.0, 12500.0 + n_frames / 60.0 * 1000.0,
        size=(w, h), fps=60, prefer_gpu=False, style=STYLE, cinematic=cfg,
    )
    first_frame = next(render_frames(job))
    assert first_frame.shape == (h, w, 4) and first_frame[..., 3].min() == 255
    for y, x in ((4, 4), (4, w - 5), (h - 5, 4), (h - 5, w - 5)):
        assert int(first_frame[y, x, :3].sum()) > 50  # sin bordes negros en ninguna relación de aspecto

    run_export(job)
    info = ffprobe_json(out)
    (v_stream,) = info["streams"]
    assert (v_stream["codec_name"], v_stream["pix_fmt"], v_stream["width"], v_stream["height"]) == ("h264", "yuv420p", w, h)
    assert v_stream["nb_read_frames"] == str(n_frames) and v_stream["color_space"] == "bt709"
    raw_mp4 = out.read_bytes()
    assert raw_mp4.find(b"moov") < raw_mp4.find(b"mdat")


# ---------------- 12. Modo sin GPU, paridad GPU, errores de medios y ejecutable portable ----------------

def test_12_cpu_fallback_gpu_parity_media_errors_and_portable_constraints(make_window, qapp, tmp_path):
    # 12a. Errores de medios: inexistente, corrupto, solo audio y MP3 con carátula adjunta (attached pic)
    with pytest.raises(VideoError):
        probe_video(tmp_path / "no_existe.mp4")

    corrupt = tmp_path / "corrupto.mp4"
    corrupt.write_bytes(b"esto no es un contenedor mp4 valido" * 20)
    with pytest.raises(VideoError):
        probe_video(corrupt)

    audio_only = tmp_path / "solo_audio.wav"
    subprocess.run(
        [
            ffmpeg.find_ffmpeg("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(audio_only),
        ],
        check=True,
    )
    with pytest.raises(VideoError):
        probe_video(audio_only)

    cover_jpg = tmp_path / "cover.png"
    cover_jpg.write_bytes(bytes(demo_cover(128).encodeToData()))
    mp3_attached = tmp_path / "con_caratula.mp3"
    subprocess.run(
        [
            ffmpeg.find_ffmpeg("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-i", str(cover_jpg),
            "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-c:v", "png",
            "-id3v2_version", "3", "-disposition:v:0", "attached_pic", str(mp3_attached),
        ],
        check=True,
    )
    with pytest.raises(VideoError):
        probe_video(mp3_attached)

    # 12b. Operación sin GPU (prefer_gpu=False) en ventana + manejo de error en UI + video borrado tras importar
    w_cpu = make_window("cpu.ini", gpu=False)
    w_cpu.show()
    w_cpu.load_demo()
    e_cpu = w_cpu.editor
    assert e_cpu.preview.ctx.gpu is False

    toasts_before = w_cpu.toasts.count()
    e_cpu.import_video(corrupt)
    assert until(lambda: not e_cpu._video_busy)
    assert e_cpu.btn_video.isEnabled() and e_cpu.project.cinematic.primary_video is None
    assert w_cpu.toasts.count() >= toasts_before + 1

    transient_vid = make_video(tmp_path / "efimero.mp4", warm_quadrant_frames(30, 180, 320), fps="30")
    res = probe_video(transient_vid)
    cfg = make_cfg(res, enabled=True)
    cfg.add_interval(11000.0, 15000.0, video_start_ms=0.0, video_end_ms=1000.0)
    transient_vid.unlink()  # el usuario movió o borró el video: fallback limpio a la portada animada

    base_px = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE).snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType)
    s_missing = Scene(exact_lyrics(), demo_cover(), SIZE, style=STYLE, gpu=False, cinematic=cfg, async_video=False)
    try:
        fallback_px = s_missing.snapshot(12500.0).toarray(colorType=skia.kRGBA_8888_ColorType)
        assert np.array_equal(fallback_px, base_px)
    finally:
        s_missing.close()
    w_cpu.close()

    # 12c. Compatibilidad con ejecutable portable: sin importar QtMultimedia ni invocar ffprobe en marea/
    assert "PySide6.QtMultimedia" not in sys.modules
    marea_root = Path(__file__).resolve().parent.parent / "marea"
    for py_file in marea_root.rglob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:from|import)\s+.*QtMultimedia", src, re.M), py_file
        assert 'find_ffmpeg("ffprobe")' not in src and "find_ffmpeg('ffprobe')" not in src, py_file

    # 12d. Paridad GPU vs CPU en proceso real con OpenGL (si la máquina dispone de OpenGL)
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).parent / "cinematic_gpu_check.py")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    out = r.stdout.strip()
    if not out.startswith("SKIP"):
        assert r.returncode == 0 and out.startswith("OK"), out + "\n" + r.stderr[-600:]


# ---------------- 13. Generación dinámica de recortes, variación controlada y coherencia musical ----------------

def _synth_structured_song(
    duration_s: float,
    bpm: float,
    sections: list[tuple[float, float, float]],
    sr: int = local.RATE,
) -> tuple[Lyrics, np.ndarray, list[float]]:
    """Sintetiza letra estructurada y pista de audio con pulso rítmico real, cambios de sección y pausas."""
    n = int(round(duration_s * sr))
    t = np.arange(n, dtype=np.float32) / sr
    beat_s = 60.0 / bpm
    env = np.full(n, 0.08, dtype=np.float32)
    event_anchors_ms: list[float] = []

    for s0, s1, amp in sections:
        mask = (t >= s0) & (t < s1)
        env[mask] = amp
        event_anchors_ms.append(s0 * 1000.0)
        event_anchors_ms.append(s1 * 1000.0)

    # Pulsos rítmicos reales en la banda vocal/armónica (220/440/880 Hz) modulados por cada tiempo del compás
    beat_phase = np.mod(t, beat_s) / beat_s
    pulse = (0.35 + 0.65 * np.exp(-5.5 * beat_phase)).astype(np.float32)
    carrier = (
        0.60 * np.sin(2.0 * np.pi * 220.0 * t)
        + 0.30 * np.sin(2.0 * np.pi * 440.0 * t)
        + 0.15 * np.sin(2.0 * np.pi * 880.0 * t)
    ).astype(np.float32)
    mono = (env * pulse * carrier * 0.75).astype(np.float32)

    # Construir versos en las secciones con energía vocal activa (amp >= 0.20) dejando interludios en pausas/intros
    lines: list[Line] = []
    refrain_words = ("vuel", "ve", "la", "ma", "re", "a")
    sec_idx = 0
    for s0, s1, amp in sections:
        if amp < 0.20 or (s1 - s0) < 2.2:
            continue
        sec_idx += 1
        is_chorus = amp >= 0.68
        phrase_step = max(2.0, min(4.2, 4.0 * beat_s))
        cur = s0 + 0.25
        line_k = 0
        while cur + 1.5 <= s1 - 0.15:
            end_l = min(s1 - 0.1, cur + phrase_step * 0.78)
            dur_l = end_l - cur
            words = refrain_words if (is_chorus and line_k == 0) else (f"sec{sec_idx}", f"lin{line_k}", "luz", "mar")
            step_w = dur_l / len(words)
            syls = []
            for wi, wtxt in enumerate(words):
                b_ms = round((cur + wi * step_w) * 1000.0)
                e_ms = round((cur + (wi + 1) * step_w - 0.02) * 1000.0)
                if is_chorus and wi == len(words) - 1:
                    e_ms = round((cur + dur_l + 0.25) * 1000.0)  # nota sostenida expresiva en cierre de frase
                syls.append(Syllable(wtxt, b_ms, max(b_ms + 80, e_ms), True))
            lines.append(Line(syls[0].begin, syls[-1].end, syllables=syls, bg=(is_chorus and line_k % 2 == 1)))
            event_anchors_ms.append(float(syls[0].begin))
            cur += phrase_step
            line_k += 1

    lyrics = Lyrics(lines=lines)
    for il in lyrics.interludes:
        event_anchors_ms.extend([float(il.start), float(il.end)])
    return lyrics, mono, sorted(set(event_anchors_ms))


def test_13_dynamic_trim_counts_durations_and_controlled_variation(win, tmp_path):
    # 13a. Canciones distintas en duración, estructura y tempo producen cantidades distintas de recortes (sin techo fijo de 2)
    ly_short, au_short, _ = _synth_structured_song(
        20.0, 72.0,
        [(0.0, 4.2, 0.05), (4.2, 18.5, 0.34), (18.5, 20.0, 0.08)],
    )
    ly_mid, au_mid, anchors_mid = _synth_structured_song(
        58.0, 108.0,
        [
            (0.0, 5.0, 0.06), (5.0, 17.0, 0.32), (17.0, 19.6, 0.02),
            (19.6, 34.0, 0.78), (34.0, 37.5, 0.03), (37.5, 53.0, 0.82), (53.0, 58.0, 0.05),
        ],
    )
    ly_full, au_full, _ = _synth_structured_song(
        165.0, 134.0,
        [
            (0.0, 7.0, 0.05), (7.0, 26.0, 0.34), (26.0, 28.5, 0.02),
            (28.5, 52.0, 0.80), (52.0, 60.0, 0.04), (60.0, 82.0, 0.38),
            (82.0, 106.0, 0.84), (106.0, 114.0, 0.02), (114.0, 132.0, 0.45),
            (132.0, 156.0, 0.90), (156.0, 165.0, 0.05),
        ],
    )

    ivs_short = suggest_intervals(ly_short, 25_000, audio_samples=au_short, range_ms=(0.0, 20_000.0))
    ivs_mid = suggest_intervals(ly_mid, 65_000, audio_samples=au_mid, range_ms=(0.0, 58_000.0))
    ivs_full = suggest_intervals(ly_full, 175_000, audio_samples=au_full, range_ms=(0.0, 165_000.0))

    assert 1 <= len(ivs_short) <= 3
    assert len(ivs_mid) > 2 and len(ivs_mid) > len(ivs_short)
    assert len(ivs_full) >= 8 and len(ivs_full) > len(ivs_mid) + 2
    assert len({len(ivs_short), len(ivs_mid), len(ivs_full)}) == 3

    # 13b. Duraciones variadas y distribución natural a lo largo de toda la canción (sin amontonarse en un solo tramo)
    durs_full = [iv.duration_ms for iv in ivs_full]
    assert max(durs_full) - min(durs_full) >= 1100.0
    assert len({round(d / 250.0) for d in durs_full}) >= 4  # múltiples duraciones musicales distintas

    early = [iv for iv in ivs_full if iv.start_ms < 55_000.0]
    middle = [iv for iv in ivs_full if 55_000.0 <= iv.start_ms < 110_000.0]
    late = [iv for iv in ivs_full if iv.start_ms >= 110_000.0]
    assert len(early) >= 2 and len(middle) >= 2 and len(late) >= 2

    # 13c. Aleatoriedad controlada entre generaciones manteniendo coherencia musical con pulsos, pausas y secciones
    analysis_mid = analyze_audio(au_mid)
    musical_refs = sorted(
        set(anchors_mid)
        | {t for t, _ in analysis_mid.accents_ms}
        | set(analysis_mid.beats_ms)
        | {p0 for p0, _ in analysis_mid.pauses_ms}
    )
    generations = [
        suggest_intervals(ly_mid, 65_000, audio_samples=au_mid, range_ms=(0.0, 58_000.0), seed=s)
        for s in range(5)
    ]
    signatures = {tuple((round(iv.start_ms), round(iv.end_ms), round(iv.video_start_ms)) for iv in g) for g in generations}
    assert len(signatures) >= 3  # generaciones sucesivas producen secuencias genuinamente variadas

    for gen in generations:
        assert len(gen) >= 3
        for iv in gen:
            # Cada corte sigue anclado cerca de una referencia musical real (pulso, pausa, transición o frase), no arbitrario
            nearest_ref = min(abs(iv.start_ms - r) for r in musical_refs)
            assert nearest_ref <= 1100.0, (iv, nearest_ref)

    # En la UI del editor, pulsar «Sugerir» repetidamente rota la semilla sobre la canción completa
    vid = make_video(tmp_path / "var_ui.mp4", warm_quadrant_frames(90, 240, 180), fps="30")
    e = win.editor
    e.cmb_source.setCurrentIndex(1)
    e.project.lyrics = ly_mid
    e.project.audio_samples = np.column_stack([au_mid, au_mid])
    e.import_video(vid)
    assert until(lambda: not e._video_busy and e.project.cinematic.primary_video is not None)

    e._suggest_cinematic()
    seq_ui_1 = [(iv.start_ms, iv.end_ms) for iv in e.project.cinematic.intervals]
    e._suggest_cinematic()
    seq_ui_2 = [(iv.start_ms, iv.end_ms) for iv in e.project.cinematic.intervals]
    assert len(seq_ui_1) > 2 and len(seq_ui_2) > 2
    assert seq_ui_1 != seq_ui_2
    assert any(s >= 30_000.0 for s, _ in seq_ui_1)  # cubre la canción entera, no solo los primeros 6 versos


# ---------------- 14. Sincronización guiada por la progresión del audio ----------------

def test_14_audio_driven_video_synchronization_and_progression():
    ly, au, _ = _synth_structured_song(
        120.0, 118.0,
        [
            (0.0, 6.0, 0.05), (6.0, 24.0, 0.34), (24.0, 26.8, 0.02),
            (26.8, 48.0, 0.78), (48.0, 53.0, 0.03), (53.0, 76.0, 0.40),
            (76.0, 100.0, 0.85), (100.0, 114.0, 0.88), (114.0, 120.0, 0.04),
        ],
    )
    res = VideoResource(id="vid_sync", path="virtual.mp4", duration_ms=128_000, width=1920, height=1080, fps_num=30, fps_den=1)
    open_ms = compute_opening_trim_ms(res.duration_ms, 120_000.0, resource=res)
    assert open_ms == pytest.approx(8000.0, abs=100.0)

    ivs = suggest_intervals(
        ly, res.duration_ms, audio_samples=au, range_ms=(0.0, 120_000.0),
        resource=res, opening_trim_ms=open_ms, song_duration_ms=120_000.0,
    )
    assert len(ivs) >= 6

    # Los segmentos de video avanzan monótonamente siguiendo la línea de tiempo de la canción a velocidad 1:1
    song_mids = np.array([0.5 * (iv.start_ms + iv.end_ms) for iv in ivs])
    vid_mids = np.array([0.5 * (iv.video_start_ms + iv.video_end_ms) for iv in ivs])
    for prev_iv, next_iv in zip(ivs, ivs[1:]):
        assert prev_iv.start_ms < next_iv.start_ms
        assert prev_iv.video_start_ms < next_iv.video_start_ms
    for iv in ivs:
        assert iv.video_start_ms >= open_ms - 10.0
        assert iv.video_end_ms < res.duration_ms  # margen seguro antes del último PTS: evita frames congelados al final
        assert iv.video_duration_ms == pytest.approx(iv.duration_ms, abs=25.0)

    corr = float(np.corrcoef(song_mids, vid_mids)[0, 1])
    assert corr > 0.94

    # Incluso si se pide solo un tramo temprano de la canción [6..32 s], el video no salta al final (créditos) del videoclip
    early_ivs = suggest_intervals(
        ly, res.duration_ms, audio_samples=au, range_ms=(6_000.0, 32_000.0),
        resource=res, opening_trim_ms=open_ms, song_duration_ms=120_000.0,
    )
    assert len(early_ivs) >= 1
    assert all(iv.video_end_ms <= 52_000.0 for iv in early_ivs)


# ---------------- 15. Recorte inicial del video y coincidencia exacta de duración con la canción ----------------

def test_15_opening_video_trim_and_exact_soundtrack_export_duration_match(win, tmp_path):
    from marea.providers.spotify import Track

    # Videoclip de 9.0 s (180 cuadros a 20 fps) frente a pista musical completa de 5.50 s:
    # - [0.0 .. 1.0 s] (0..20): cuadros negros iniciales (leader)
    # - [1.0 .. 3.5 s] (20..70): introducción azul previa a la música
    # - Corte de plano limpio en 3.50 s (cuadro 70) -> [3.5 .. 9.0 s] (70..180): metraje cálido activo en movimiento (5.5 s)
    fps = 20
    n_v = fps * 9
    h, w = 180, 140
    v_frames = np.zeros((n_v, h, w, 4), dtype=np.uint8)
    v_frames[..., 3] = 255
    # [0..20] queda en negro puro (0, 0, 0)
    for i in range(20, 70):
        v_frames[i, ..., :3] = (25, 65, 215 + (i % 15))  # intro azul
    for i in range(70, n_v):
        band = (i * 5) % h
        v_frames[i, ..., :3] = (232, 138 + (i % 35), 38)  # metraje cálido principal
        v_frames[i, band : min(h, band + 22), :, :3] = (250, 205, 85)  # movimiento continuo real (cero cuadros congelados)

    mv_path = make_video(tmp_path / "videoclip_con_intro.mp4", v_frames, fps=str(fps))
    res = probe_video(mv_path)
    assert res.duration_ms == pytest.approx(9000.0, abs=150.0)
    assert res.visual_start_ms == pytest.approx(1000.0, abs=300.0)  # detecta y descarta el segundo inicial en negro
    assert any(abs(c - 3500) <= 300 for c in res.scene_cuts_ms), res.scene_cuts_ms

    # Pista musical completa de exactamente 5.50 s (nunca debe recortarse aunque `project.track` tuviera metadatos más cortos)
    song_s = 5.50
    sr = local.RATE
    t_a = np.arange(int(round(song_s * sr)), dtype=np.float32) / sr
    env_a = np.where((t_a >= 1.8) & (t_a < 2.9), 0.02, 0.65).astype(np.float32)
    pulse_a = (0.4 + 0.6 * np.exp(-6.0 * np.mod(t_a, 0.5))).astype(np.float32)
    mono_a = (env_a * pulse_a * np.sin(2.0 * np.pi * 220.0 * t_a)).astype(np.float32)
    stereo_a = np.column_stack([mono_a, mono_a])

    short_ly = Lyrics([
        Line(400, 1750, syllables=[Syllable("Sol", 400, 1050, True), Syllable("mar", 1070, 1750, True)]),
        Line(3000, 4950, syllables=[Syllable("luz", 3000, 3950, True), Syllable("vuel", 3980, 4950, True)]),
    ])

    e = win.editor
    e.settings.setValue("output_dir", str(tmp_path))
    e.settings.setValue("export/preset", "tiktok")
    e.project.lyrics = short_ly
    # Simulamos metadato previo más corto (4.1 s) para probar que el audio local real (5.50 s) prevalece y no se trunca
    e.project.track = Track("id_stale", "Prueba", "Artista", "Album", 4100, "")
    e.cmb_source.setCurrentIndex(1)  # Archivo de audio local
    e.project.audio_samples = stereo_a
    e.project.audio_offset_ms = 0.0

    assert e._full_end_ms() == pytest.approx(5500.0, abs=5.0)

    e.import_video(mv_path)
    assert until(lambda: not e._video_busy and e.project.cinematic.primary_video is not None)

    # El recorte inicial recorta la apertura excedente (~3.5 s: leader negro + intro azul) y aterriza tras el corte limpio de 3.5 s
    open_trim = e.project.cinematic.opening_trim_ms
    assert 3300.0 <= open_trim <= 3850.0, open_trim
    assert e.sp_open_trim.value() == pytest.approx(open_trim / 1000.0, abs=0.02)

    e._suggest_cinematic()
    assert len(e.project.cinematic.intervals) >= 1
    for iv in e.project.cinematic.intervals:
        assert iv.video_start_ms >= open_trim - 15.0  # jamás entra en el leader negro ni en la intro azul descartada
        assert iv.video_end_ms <= res.duration_ms

    # Exportamos en modo «Canción completa» a tamaño ligero verificando que video y audio duran exactamente 5.50 s
    iv_peak = e.project.cinematic.intervals[0]
    t_mid_s = 0.5 * (iv_peak.start_ms + iv_peak.end_ms) / 1000.0
    out_full = tmp_path / "completa_sincronizada.mp4"
    tr_clear = CinematicTreatment(opacity=1.0, blur=0.05, dim=0.05, vignette=0.05, grain=0.0, saturation=1.0, contrast=1.0)
    for iv in e.project.cinematic.intervals:
        iv.treatment = copy.copy(tr_clear)

    job_full = ExportJob(
        short_ly, e.project.cover_png(), out_full, 0.0, e._full_end_ms(),
        size=(144, 256), fps=30, prefer_gpu=False, style=STYLE, cinematic=copy.deepcopy(e.project.cinematic),
    )
    audio_full = AudioSpec("local", 0.0, e._full_end_ms(), samples=stereo_a, offset_ms=0.0, fade_out_ms=0.0)
    item = QueueItem("Prueba", "Artista", e.project.cover_png(), job_full, audio_full)
    win.queue.add(item)
    assert until(lambda: item.state not in ("queued", "audio", "render"), timeout_ms=60_000)
    assert item.state == DONE, item.error

    info = ffprobe_json(out_full)
    streams = {s["codec_name"]: s for s in info["streams"]}
    assert sorted(streams) == ["aac", "h264"]
    v_dur = float(streams["h264"]["duration"])
    a_dur = float(streams["aac"]["duration"])
    fmt_dur = float(info["format"]["duration"])

    assert v_dur == pytest.approx(song_s, abs=0.05)
    assert a_dur == pytest.approx(song_s, abs=0.06)
    assert fmt_dur == pytest.approx(song_s, abs=0.06)
    assert abs(v_dur - a_dur) <= 0.05
    assert int(streams["h264"]["nb_read_frames"]) == round(song_s * 30)

    # Verificamos los cuadros exportados: sin cuadros negros iniciales/finales y con movimiento real del metraje cálido (R > B)
    raw_sample = subprocess.run(
        [
            ffmpeg.find_ffmpeg("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, t_mid_s - 0.15):.3f}", "-i", str(out_full), "-an",
            "-frames:v", "6", "-f", "rawvideo", "-pix_fmt", "rgba", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    fb = 144 * 256 * 4
    n_chk = len(raw_sample) // fb
    assert n_chk >= 4
    chk_frames = np.frombuffer(raw_sample[: n_chk * fb], dtype=np.uint8).reshape((n_chk, 256, 144, 4))
    for k in range(n_chk):
        mean_rgb = chk_frames[k, ..., :3].mean(axis=(0, 1))
        assert mean_rgb.mean() > 45.0  # jamás negro
        assert mean_rgb[0] > mean_rgb[2] + 35.0  # metraje cálido (ámbar), no la intro azul recortada
    diffs = [float(np.abs(chk_frames[k + 1].astype(int) - chk_frames[k].astype(int)).mean()) for k in range(n_chk - 1)]
    assert max(diffs) > 0.4  # metraje vivo, no congelado
