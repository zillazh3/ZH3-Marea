"""Export con canal alfa: WebM VP9, ProRes 4444 y secuencia PNG sobre fondo transparente."""
import json
import subprocess

import numpy as np
import pytest

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export.ffmpeg import ALPHA_TARGETS, build_command
from marea.export.job import ExportJob, clip_name, render_frames, run_export
from marea.export import history
from marea.model.background import BackgroundConfig
from marea.render.scene import Style
from marea.sysutil import NO_WINDOW, find_ffmpeg

W, H, FPS = 180, 320, 30
START = 12500


def alpha_job(tmp_path, target, name, frames=30, **kw):
    kw.setdefault("style", Style(title="Marea de luz", artist="Demo"))
    return ExportJob(exact_lyrics(), bytes(demo_cover().encodeToData()), tmp_path / name, START, START + frames / FPS * 1000,
                     size=(W, H), fps=FPS, prefer_gpu=False, background=BackgroundConfig(source="transparent"),
                     target=target, **kw)


def ffprobe_pix_fmt(path, vcodec=None):
    cmd = [find_ffmpeg("ffprobe"), "-v", "error", *(["-c:v", vcodec] if vcodec else []), "-select_streams", "v:0",
           "-show_entries", "stream=pix_fmt,codec_name", "-of", "json", str(path)]
    s = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=NO_WINDOW).stdout)["streams"][0]
    return s["pix_fmt"], s["codec_name"]


def decode_rgba(src, vcodec=None):
    cmd = [find_ffmpeg(), "-v", "error", *(["-c:v", vcodec] if vcodec else []), "-i", str(src), "-f", "rawvideo",
           "-pix_fmt", "rgba", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, creationflags=NO_WINDOW).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W, 4)


def stage_part(frame):
    return frame[int(0.17 * H):]  # debajo del encabezado (la portada en miniatura sí tiene color propio)


def assert_alpha_content(frames):
    f = frames[len(frames) // 2]
    assert f[0, 0, 3] == 0 and f[0, W - 1, 3] == 0  # esquinas transparentes
    assert max(fr[..., 3].max() for fr in frames) > 100  # hay texto/brillo opaco en algún momento
    st = stage_part(frames[-1])
    assert (st[..., 3] > 20).sum() > 50


def test_command_lines_for_alpha_targets(tmp_path):
    web = " ".join(build_command(tmp_path / "o.webm", W, H, 60, target="webm", audio=tmp_path / "a.wav"))
    for part in ("libvpx-vp9", "-pix_fmt yuva420p", "libopus"):
        assert part in web
    pro = " ".join(build_command(tmp_path / "o.mov", W, H, 60, "nvenc", target="prores", audio=tmp_path / "a.wav"))
    assert "prores_ks" in pro and "-profile:v 4444" in pro and "yuva444p10le" in pro and "pcm_s16le" in pro and "nvenc" not in pro
    png = build_command(tmp_path / "seq", W, H, 60, target="png", audio=tmp_path / "a.wav")
    assert "-c:a" not in png and str(png[-1]).endswith("seq_%05d.png")


def test_clip_name_per_target_and_tag():
    assert clip_name("A", "B", 0, 1, "webm").endswith("[ZH3 Marea].webm")
    assert clip_name("A", "B", 0, 1, "prores").endswith("[ZH3 Marea].mov")
    assert clip_name("A", "B", 0, 1, "png").endswith("[ZH3 Marea]")  # carpeta
    assert clip_name("A", "B", 0, 1, tag="Pleamar") == "A - B (versos 1–2) [Pleamar] [ZH3 Marea].mp4"
    assert set(ALPHA_TARGETS) == {"webm", "prores", "png"}


def test_transparent_frames_have_no_background_and_no_dark_fringe(tmp_path):
    frames = list(render_frames(alpha_job(tmp_path, "png", "seq")))
    assert_alpha_content(frames)
    # Bordes semitransparentes del texto/brillo: el color sin premultiplicar sigue siendo claro (no negro)
    edge = np.concatenate([stage_part(f).reshape(-1, 4) for f in frames[::5]])
    edge = edge[(edge[:, 3] > 26) & (edge[:, 3] < 230)]
    assert len(edge) > 200
    luma = edge[:, :3] @ np.array([0.2126, 0.7152, 0.0722])
    assert np.median(luma) > 200 and np.percentile(luma, 5) > 110


def test_transparent_render_has_no_wordfx_black_halo_from_watermark(tmp_path):
    """Con el fondo transparente la marca de agua no lleva sombra negra: todos sus píxeles son claros."""
    frames = list(render_frames(alpha_job(tmp_path, "png", "seq", frames=20, style=Style(wm_text="Zilla-ZH3"))))
    wm = frames[-1][int(0.80 * H):int(0.86 * H)]
    px = wm.reshape(-1, 4)
    px = px[px[:, 3] > 8]
    assert len(px) > 5 and (px[:, :3].max(axis=1) > 180).all()


@pytest.mark.parametrize("target,name,vcodec,pix", [("webm", "o.webm", "libvpx-vp9", "yuva420p"),
                                                    ("prores", "o.mov", None, "yuva")])
def test_alpha_video_targets(tmp_path, target, name, vcodec, pix):
    out = run_export(alpha_job(tmp_path, target, name))
    assert out.is_file() and out.stat().st_size > 1000
    got, codec = ffprobe_pix_fmt(out, vcodec)
    assert got.startswith(pix) or got == pix  # yuva420p / yuva444p12le según el decodificador
    assert_alpha_content(decode_rgba(out, vcodec))


def test_png_sequence_with_sidecar_and_thumbnail(tmp_path):
    out = run_export(alpha_job(tmp_path, "png", "seq", frames=12))
    pngs = sorted(out.glob("*.png"))
    assert len(pngs) == 12 and pngs[0].name == "seq_00001.png"
    meta = json.loads((out / "marea.json").read_text(encoding="utf-8"))
    assert (meta["fps"], meta["width"], meta["height"], meta["frames"]) == (FPS, W, H, 12)
    assert meta["start_ms"] == START and meta["alpha"] == "straight"
    assert_alpha_content(decode_rgba(out / "seq_%05d.png"))
    thumb = history.make_thumbnail(out, tmp_path / "thumbs", at_s=0.2)
    assert thumb is not None and thumb.is_file()


def test_failed_png_export_leaves_no_folder(tmp_path):
    from marea.export.job import ExportCancelled
    with pytest.raises(ExportCancelled):
        run_export(alpha_job(tmp_path, "png", "seq"), cancelled=lambda: True)
    assert not (tmp_path / "seq").exists()


def test_png_target_refuses_existing_folder(tmp_path):
    (tmp_path / "seq").mkdir()
    with pytest.raises(RuntimeError):
        run_export(alpha_job(tmp_path, "png", "seq", frames=2))
    assert (tmp_path / "seq").is_dir()  # la carpeta ajena no se toca


# ---------------- editor ----------------

@pytest.fixture
def win(make_window, qapp):
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    return w


def test_picking_an_alpha_target_switches_background_and_builds_matching_job(win, qapp):
    e = win.editor
    assert e.project.background.source == "cover"
    e.target_actions["webm"].trigger()
    qapp.processEvents()
    assert e.project.background.source == "transparent" and e.cmb_bgsrc.currentData() == "transparent"
    assert e.preview.scene.background.transparent
    job = e.build_job()
    assert job.target == "webm" and job.background.transparent and job.out_path.name.endswith(".webm")
    # volver a un fondo con imagen devuelve el formato a MP4
    e.cmb_bgsrc.setCurrentIndex(e.cmb_bgsrc.findData("cover"))
    qapp.processEvents()
    assert e.settings.value("export/target") == "mp4" and e.build_job().target == "mp4"


def test_alpha_watermark_toggle_and_mp4_with_transparent_background(win):
    e = win.editor
    e.target_actions["prores"].trigger()
    assert e.build_job().style.wm_text == e.style.wm_text  # por defecto, incluida
    e.act_alpha_wm.setChecked(False)
    assert e.build_job().style.wm_text == "" and e.style.wm_text != ""
    e.target_actions["mp4"].trigger()
    assert e.project.background.transparent  # el fondo sigue transparente...
    job = e.build_job()
    assert job.target == "mp4" and job.background.source == "cover"  # ...pero un MP4 no puede llevarlo
