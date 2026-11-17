"""Encoder backends (CPU, NVIDIA, AMD, Intel) x codecs (H.264, HEVC, AV1): command building, detection, fallbacks, UI."""
import pytest
from PySide6.QtCore import QSettings

from marea.export import ffmpeg
from tests.test_export import job, probe, run_export

ALL = [(b, c) for b in ffmpeg.BACKENDS for c in ffmpeg.CODECS]
FFMPEG_NAME = {"h264": "h264", "hevc": "hevc", "av1": "av1"}


@pytest.fixture
def detected(monkeypatch):
    """Pretend the machine has exactly these encoders (no ffmpeg probing)."""
    def set_to(found: dict):
        monkeypatch.setattr(ffmpeg, "_detected", {b: tuple(found.get(b, ())) for b in ffmpeg.BACKENDS})
    return set_to


@pytest.mark.parametrize("backend,codec", ALL)
def test_every_backend_codec_has_a_complete_command(tmp_path, backend, codec):
    cmd = ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60, backend, codec=codec)
    joined = " ".join(cmd)
    name, pix, _ = ffmpeg._ENCODERS[backend, codec]
    assert f"-c:v {name}" in joined and f"-pix_fmt {pix}" in joined and f"format={pix}" in joined
    assert "+faststart" in joined and "bt709" in joined
    assert ("-tag:v hvc1" in joined) == (codec == "hevc")  # QuickTime/iOS only open HEVC-in-MP4 tagged hvc1


def test_h264_cpu_is_the_default_command_and_unknown_combos_are_rejected(tmp_path):
    cmd = ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60)
    assert "libx264" in cmd and "-crf" in cmd
    with pytest.raises(ValueError):
        ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60, "tpu", codec="h264")
    with pytest.raises(ValueError):
        ffmpeg.build_command(tmp_path / "o.mp4", 1080, 1920, 60, "cpu", codec="vp8")


def test_alpha_targets_ignore_backend_and_codec(tmp_path):
    web = " ".join(ffmpeg.build_command(tmp_path / "o.webm", 64, 64, 30, "amf", target="webm", codec="av1"))
    assert "libvpx-vp9" in web and "amf" not in web and "av1" not in web


def test_detection_only_keeps_listed_encoders_that_really_encode(monkeypatch):
    monkeypatch.setattr(ffmpeg, "_detected", None)
    monkeypatch.setattr(ffmpeg, "_listed_encoders", lambda: " libx264 libx265 h264_nvenc hevc_nvenc av1_nvenc h264_amf hevc_amf h264_qsv ")
    monkeypatch.setattr(ffmpeg, "_probe", lambda b, c: (b, c) not in {("nvenc", "av1"), ("amf", "h264"), ("amf", "hevc")})
    found = ffmpeg.detect_encoders()
    assert found == {"cpu": ("h264", "hevc"), "nvenc": ("h264", "hevc"), "amf": (), "qsv": ("h264",)}
    assert ffmpeg.detected_encoders() is found


def test_detection_without_ffmpeg_offers_nothing_and_resolves_to_cpu_h264(monkeypatch):
    monkeypatch.setattr(ffmpeg, "_detected", None)
    monkeypatch.setattr(ffmpeg, "_listed_encoders", lambda: "")
    assert ffmpeg.detect_encoders() == {b: () for b in ffmpeg.BACKENDS}
    assert ffmpeg.resolve_encoder("auto", "hevc") == ("cpu", "h264")


def test_resolve_auto_prefers_nvidia_then_amd_then_intel(detected):
    detected({"cpu": ["h264", "hevc", "av1"], "nvenc": ["h264", "hevc"], "amf": ["h264", "hevc", "av1"], "qsv": ["h264", "av1"]})
    assert ffmpeg.resolve_encoder("auto", "h264") == ("nvenc", "h264")
    assert ffmpeg.resolve_encoder("auto", "av1") == ("amf", "av1")  # NVENC here has no AV1: the next GPU that does
    detected({"cpu": ["h264"], "qsv": ["h264", "hevc"]})  # Intel Arc / iGPU only
    assert ffmpeg.resolve_encoder("auto", "hevc") == ("qsv", "hevc")
    assert ffmpeg.default_encoder() == "qsv"
    detected({"cpu": ["h264", "hevc", "av1"]})
    assert ffmpeg.default_encoder() == "cpu" and ffmpeg.resolve_encoder("auto", "av1") == ("cpu", "av1")


def test_resolve_explicit_choice_falls_back_to_cpu_then_to_h264(detected):
    detected({"cpu": ["h264", "hevc"], "amf": ["h264"]})
    assert ffmpeg.resolve_encoder("amf", "h264") == ("amf", "h264")
    assert ffmpeg.resolve_encoder("amf", "hevc") == ("cpu", "hevc")  # this GPU cannot: CPU keeps the codec
    assert ffmpeg.resolve_encoder("nvenc", "h264") == ("cpu", "h264")  # that GPU is not here
    assert ffmpeg.resolve_encoder("amf", "av1") == ("cpu", "h264")  # nobody can: the universal codec
    assert ffmpeg.resolve_encoder("auto", "vp8") == ("amf", "h264")  # junk setting values are tolerated


@pytest.mark.parametrize("backend,codec", ALL)
def test_each_detected_encoder_really_produces_that_codec(tmp_path, backend, codec):
    if codec not in ffmpeg.detect_encoders()[backend]:
        pytest.skip(f"{backend}/{codec} no disponible en este equipo")
    info = probe(run_export(job(tmp_path, frames=12, encoder=backend, codec=codec)))
    assert info["streams"][0]["codec_name"] == FFMPEG_NAME[codec]


def test_settings_lists_only_detected_encoders_and_keeps_a_missing_choice_stored(make_window, qapp, detected, tmp_path):
    detected({"cpu": ["h264", "hevc"], "nvenc": ["h264", "hevc"]})
    before = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)
    before.setValue("export/encoder", "amf")  # the GPU the user had in a previous session; it is gone now
    before.sync()
    win = make_window()
    assert win.settings_body.encoder.currentData() == "amf"
    body = win.settings_body
    body._encoders_detected(ffmpeg.detect_encoders())
    enabled = {k: body.encoder.model().item(body.encoder.findData(k)).isEnabled() for k in ("auto", *ffmpeg.BACKENDS)}
    assert enabled == {"auto": True, "cpu": True, "nvenc": True, "amf": False, "qsv": False}
    assert "H.264" in body.encoder.itemText(body.encoder.findData("nvenc")) and "AV1" not in body.encoder.itemText(body.encoder.findData("nvenc"))
    assert "no detectado" in body.encoder.itemText(body.encoder.findData("amf"))
    assert "NVIDIA NVENC" in body.encoder_note.text()
    assert body.encoder.currentData() == "auto" and win.settings.value("export/encoder") == "amf"  # shown as Automático, not overwritten


def test_editor_codec_menu_defaults_to_h264_and_feeds_the_job(make_window, qapp, detected):
    detected({"cpu": ["h264", "hevc"], "nvenc": ["h264"]})
    win = make_window()
    win.load_demo()
    e = win.editor
    assert e.codec_actions["h264"].isChecked() and e.summary.text() == "MP4 · H.264"
    assert e.build_job().codec == "h264" and e.build_job().encoder == "nvenc"
    e.menu.aboutToShow.emit()
    assert not e.codec_actions["av1"].isEnabled() and e.codec_actions["hevc"].isEnabled()  # nothing here encodes AV1
    shown = []
    e.notify = lambda text, kind="info", action=None: shown.append(text)
    e.codec_actions["hevc"].trigger()
    job_ = e.build_job()
    assert (job_.encoder, job_.codec) == ("cpu", "hevc") and e.summary.text() == "MP4 · HEVC (H.265)"
    assert shown == []  # Automático falls back silently: it never promised a GPU
    win.settings.setValue("export/encoder", "nvenc")
    e.build_job()
    e.build_job()  # the warning shows once per situation, not once per job
    assert sum("NVIDIA NVENC" in t for t in shown) == 1
    e.target_actions["webm"].trigger()
    e.menu.aboutToShow.emit()
    assert not e.codec_menu.isEnabled()  # WebM brings its own codec
    assert e.build_job().codec == "h264"
