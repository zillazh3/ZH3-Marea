"""A project file from someone else must never make us touch a network path (Windows would send NTLM credentials)."""
import os

import pytest

from marea.model import background as bg
from marea.model import cinematic as cine
from marea.project import demo_project, project_from_dict, project_to_dict
from marea.sysutil import is_local_path

BS = "\\"


@pytest.mark.parametrize("path,local", [
    ("C:" + BS + "a" + BS + "b.wav", True), ("C:/a/b.wav", True), ("rel/a.wav", True), ("", True),
    ("//evil/share/a.wav", False), (BS * 2 + "evil" + BS + "share" + BS + "a", False),
    (BS * 2 + "?" + BS + "C:" + BS + "a.wav", True), (BS * 2 + "?" + BS + "UNC" + BS + "evil" + BS + "s", False),
    (BS * 2 + "." + BS + "pipe" + BS + "x", False),
    (BS * 2 + "?" + BS + "GLOBALROOT" + BS + "Device" + BS + "Mup" + BS + "evil" + BS + "s" + BS + "v.mp4", False),
    (BS + "??" + BS + "UNC" + BS + "evil" + BS + "s" + BS + "v.mp4", False),
    ("//?/GLOBALROOT/Device/Mup/evil/s/v.mp4", False),
])
def test_is_local_path(path, local):
    assert is_local_path(path) is local


def test_project_audio_on_a_network_path_is_dropped():
    d = project_to_dict(demo_project())
    d["audio"] = {"path": "//evil.host/s/a.wav", "offset_ms": 0}
    assert project_from_dict(d)[0].audio_file is None


def test_background_video_on_a_network_path_is_never_stat_ed(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: calls.append(p) or (_ for _ in ()).throw(OSError()))
    cfg = bg.from_dict({"source": "video", "video": {"path": "//evil.host/s/v.mp4"}})
    out, warning = bg.check_video(cfg)
    assert out.source == "cover" and warning and not any("evil" in str(c) for c in calls)


def test_project_source_on_a_network_path_is_dropped():
    d = project_to_dict(demo_project())
    d["source"] = "//evil.host/s/x.ttml"
    assert project_from_dict(d)[0].source is None


def test_network_background_video_is_dropped_even_when_the_source_is_cover(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: calls.append(p) or (_ for _ in ()).throw(OSError()))
    cfg, warning = bg.check_video(bg.from_dict({"source": "cover", "video": {"path": "//evil.host/s/v.mp4"}}))
    assert cfg.video is None and warning is None and not any("evil" in str(c) for c in calls)


def test_network_background_video_with_video_source_warns_and_forgets_the_path():
    cfg, warning = bg.check_video(bg.from_dict({"source": "video", "video": {"path": "//evil.host/s/v.mp4"}}))
    assert cfg.source == "cover" and cfg.video is None and warning


def test_cinematic_resource_on_a_network_path_is_cleared():
    cfg = cine.from_dict({"resources": {"r1": {"id": "r1", "path": "//evil.host/s/v.mp4"}}})
    assert all(r.path == "" for r in cfg.resources.values())
