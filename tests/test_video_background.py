"""Fondo de video propio: mapeo determinista t -> cuadro, VFR, rotación, export reproducible y respaldo al abrir."""
import hashlib
import json
import os
import subprocess

import numpy as np
import pytest

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export.job import ExportJob, render_frames
from marea.model.background import BackgroundConfig, check_video, from_dict, source_ms, to_dict
from marea.model.cinematic import VideoResource
from marea.project import ClipProject, ProjectError, load_project_file, save_project
from marea.render.cinematic import VideoDecoder, probe_video
from marea.render.scene import Style
from marea.sysutil import NO_WINDOW, find_ffmpeg


def run_ffmpeg(*args):
    subprocess.run([find_ffmpeg(), "-y", "-v", "error", *map(str, args)], check=True, creationflags=NO_WINDOW)


def make_video(path, src="testsrc2=s=96x160:r=30:d=2", extra=()):
    run_ffmpeg("-f", "lavfi", "-i", src, "-pix_fmt", "yuv420p", *extra, path)
    return path


def res(duration_ms=2000, fps=30):
    return VideoResource(id="v", path="x.mp4", duration_ms=duration_ms, fps_num=fps)


def cfg(**kw):
    return BackgroundConfig(source="video", video=res(), **kw)


# ---------------- mapeo puro ----------------

def test_source_ms_repeat_trim_and_offset():
    c = cfg(trim_in_ms=500, trim_out_ms=1500)  # ventana de 1 s
    assert source_ms(c, 10_000, 10_000) == 500
    assert source_ms(c, 10_250, 10_000) == pytest.approx(750)
    assert source_ms(c, 11_250, 10_000) == pytest.approx(750)  # vuelve a empezar
    c.offset_ms = 300
    assert source_ms(c, 10_000, 10_000) == pytest.approx(800)
    c.offset_ms = -200  # negativo: sigue dentro de la ventana, sin salirse
    assert 500 <= source_ms(c, 10_000, 10_000) < 1500


def test_source_ms_pingpong_and_hold():
    c = cfg(loop="pingpong")  # 2 s de video: va y vuelve
    assert source_ms(c, 500, 0) == pytest.approx(500)
    assert source_ms(c, 2500, 0) == pytest.approx(1500)  # de regreso
    assert source_ms(c, 4000, 0) == pytest.approx(0, abs=40)
    h = cfg(loop="hold")
    last = source_ms(h, 99_000, 0)
    assert last == pytest.approx(2000 - res().frame_duration_ms) and last < 2000  # último cuadro que existe
    assert source_ms(h, -500, 0) == 0.0


def test_source_ms_clip_longer_and_shorter_than_video():
    c = cfg()
    assert source_ms(c, 500, 0) == pytest.approx(500)  # clip más corto que el video: no llega al final
    ts = [source_ms(c, t, 0) for t in range(0, 7000, 250)]  # clip más largo: siempre dentro del video
    assert all(0 <= v < 2000 for v in ts)


def test_config_roundtrip_clamps_and_ignores_unknown_keys():
    c = BackgroundConfig(source="video", video=res(), zoom=9, dim=-1, loop="nope", fit="cover")
    d = to_dict(c)
    d["mystery"] = 1
    back = from_dict(d)
    assert back.zoom == 3.0 and back.dim == 0.0 and back.loop == "repeat" and back.video.path == "x.mp4"
    assert from_dict({"schema": "marea.background", "version": 1, "zoom": "abc"}).zoom == 1.0
    with pytest.raises(Exception):
        from_dict({"schema": "otro"})
    with pytest.raises(Exception):
        from_dict({"schema": "marea.background", "version": 99})


# ---------------- ffmpeg real ----------------

def test_vfr_source_is_normalised_to_cfr(tmp_path):
    """1 s rojo a 10 fps + 1 s azul a 30 fps (VFR): el cuadro en 0.5 s es rojo y en 1.5 s es azul."""
    vfr = tmp_path / "vfr.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "color=red:s=64x64:r=10:d=1", "-f", "lavfi", "-i", "color=blue:s=64x64:r=30:d=1",
               "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p", "-fps_mode", "vfr", vfr)
    r = probe_video(vfr)
    dec = VideoDecoder(r, (32, 32))
    try:
        def px(ms):
            img = dec.get_image(ms)
            return img.toarray()[16, 16][:3]
        red, blue = px(500), px(1500)
    finally:
        dec.close()
    assert red[0] > 200 and red[2] < 60
    assert blue[2] > 200 and blue[0] < 60


def test_rotation_metadata_swaps_probe_dimensions(tmp_path):
    src = make_video(tmp_path / "land.mp4", "testsrc2=s=128x64:r=30:d=1")
    rotated = tmp_path / "rot.mp4"
    run_ffmpeg("-display_rotation", "90", "-i", src, "-c", "copy", rotated)
    plain, rot = probe_video(src), probe_video(rotated)
    assert (plain.width, plain.height, plain.rotation) == (128, 64, 0)
    assert (rot.width, rot.height) == (64, 128) and rot.rotation in (90, 270)


def test_decoder_framing_modes_output_requested_size(tmp_path):
    video = make_video(tmp_path / "v.mp4")
    r = probe_video(video)
    for fit in ("cover", "contain"):
        dec = VideoDecoder(r, (90, 160), framing=(fit, 1.5, 0.5, -0.5))
        try:
            img = dec.get_image(300)
            assert (img.width(), img.height()) == (90, 160)
        finally:
            dec.close()


# ---------------- export determinista ----------------

def video_job(tmp_path, bg, frames=15, **kw):
    start = 12500
    return ExportJob(exact_lyrics(), bytes(demo_cover().encodeToData()), tmp_path / "o.mp4", start,
                     start + frames / 30 * 1000, size=(180, 320), fps=30, prefer_gpu=False,
                     style=Style(title="Marea de luz", artist="Demo"), background=bg, **kw)


def test_video_background_export_is_deterministic(tmp_path):
    video = make_video(tmp_path / "v.mp4")
    bg = BackgroundConfig(source="video", video=probe_video(video), scrim=0.5, dim=0.2, blur=0.1)
    runs = [[hashlib.sha256(f.tobytes()).hexdigest() for f in render_frames(video_job(tmp_path, bg))] for _ in range(2)]
    assert runs[0] == runs[1] and len(runs[0]) == 15
    cover = [hashlib.sha256(f.tobytes()).hexdigest() for f in render_frames(video_job(tmp_path, BackgroundConfig()))]
    assert cover != runs[0]  # el fondo realmente cambia


def test_video_frames_follow_the_mapping(tmp_path):
    """El fondo en el instante t muestra el cuadro del video que dicta source_ms (un video que cambia de color por segundo)."""
    video = tmp_path / "c.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "color=red:s=64x64:r=30:d=1", "-f", "lavfi", "-i", "color=lime:s=64x64:r=30:d=1",
               "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p", video)
    bg = BackgroundConfig(source="video", video=probe_video(video), dim=0.0, loop="hold")
    first = next(iter(render_frames(video_job(tmp_path, bg, frames=1))))
    last = list(render_frames(video_job(tmp_path, bg, frames=40)))[-1]  # 40 cuadros a 30 fps > 1 s: ya en el verde
    top = lambda f: f[2:8, 2:8, :3].reshape(-1, 3).mean(axis=0)  # esquina superior: sin letra ni encabezado
    assert top(first)[0] > top(first)[1] + 80  # rojo
    assert top(last)[1] > top(last)[0] + 80  # verde


# ---------------- proyecto ----------------

def test_missing_or_changed_video_falls_back_to_cover(tmp_path):
    video = make_video(tmp_path / "v.mp4", "testsrc2=s=64x64:r=30:d=1")
    r = probe_video(video)
    st = os.stat(video)
    bg = BackgroundConfig(source="video", video=r, video_size=st.st_size, video_mtime_ns=st.st_mtime_ns)
    proj = ClipProject(exact_lyrics(), "t", "a", demo_cover(), background=bg)
    path = save_project(proj, tmp_path / "p.marea.json")
    ok, _ = load_project_file(path)
    assert ok.background.source == "video" and not ok.background_warning
    os.utime(video, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # cambió
    changed, _ = load_project_file(path)
    assert changed.background.source == "cover" and "cambió" in changed.background_warning
    video.unlink()
    gone, _ = load_project_file(path)
    assert gone.background.source == "cover" and "no encuentro" in gone.background_warning
    assert check_video(BackgroundConfig())[1] is None


def test_old_projects_without_background_still_load(tmp_path):
    proj = ClipProject(exact_lyrics(), "t", "a", demo_cover())
    path = save_project(proj, tmp_path / "p.marea.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("background")
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded, _ = load_project_file(path)
    assert loaded.background.source == "cover"
    raw["background"] = {"schema": "marea.background", "version": 99}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProjectError):
        load_project_file(path)


# ---------------- color adaptativo y transparencia en la escena ----------------

def scene_with(bg, **kw):
    from marea.render.scene import Scene
    sc = Scene(exact_lyrics(), demo_cover(), (180, 320), gpu=False, background=bg, **kw)
    sc.clip_start_ms = 12500
    sc.engine.warmup(12500)
    return sc


def test_adaptive_color_samples_the_video_not_the_cover(tmp_path):
    video = tmp_path / "red.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "color=red:s=64x64:r=30:d=1", "-pix_fmt", "yuv420p", video)
    sc = scene_with(BackgroundConfig(source="video", video=probe_video(video), dim=0.0))
    try:
        r, g, b = sc._backdrop(0, 0, 180, 320)
        assert r > 150 and g < 80 and b < 80  # rojo del video; la portada demo es azul
        sc.set_background(BackgroundConfig())  # de vuelta a la portada: el muestreo cambia
        r2, g2, b2 = sc._backdrop(0, 0, 180, 320)
        assert b2 > r2
    finally:
        sc.close()


def test_transparent_scene_clears_stale_pixels_and_skips_backdrop():
    import skia
    sc = scene_with(BackgroundConfig(source="transparent"))
    surf = skia.Surface(180, 320)
    surf.getCanvas().drawColor(skia.ColorRED)  # basura de un frame anterior
    sc.render(surf.getCanvas())
    px = surf.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)
    assert px[0, 0, 3] == 0 and px[160, 5, 3] == 0
    sc.close()
