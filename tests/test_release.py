"""Versioned portable archive layout without compiling the application."""
import zipfile
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from marea.version import VERSION
from tools import build_exe


def test_release_zip_is_versioned_and_contains_the_portable_folder(tmp_path, monkeypatch):
    out = tmp_path / "dist" / "ZH3 Marea"
    out.mkdir(parents=True)
    (out / "ZH3 Marea.exe").write_bytes(b"test executable")
    (out / "LICENSE").write_text("test license", encoding="utf-8")
    monkeypatch.setattr(build_exe, "ROOT", tmp_path)
    monkeypatch.setattr(build_exe, "OUT", out)
    archive = build_exe.write_zip()
    assert archive.name == f"ZH3-Marea-v{VERSION}-portable.zip"
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        assert set(package.namelist()) == {"ZH3 Marea/ZH3 Marea.exe", "ZH3 Marea/LICENSE"}


def test_build_excludes_foreign_dll_directories_without_changing_the_parent_path(tmp_path, monkeypatch):
    out = tmp_path / "dist" / "ZH3 Marea"
    out.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    for name in ("README.md", "LEEME.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY.md", "docs/vocal-layers.md"):
        (tmp_path / name).write_text("test document", encoding="utf-8")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"test binary")
    foreign_path = str(tmp_path / "foreign-dlls")
    monkeypatch.setenv("PATH", foreign_path)
    monkeypatch.delenv("MAREA_FFMPEG", raising=False)
    monkeypatch.setattr(build_exe, "ROOT", tmp_path)
    monkeypatch.setattr(build_exe, "OUT", out)
    monkeypatch.setattr(build_exe.shutil, "which", lambda name: str(ffmpeg))
    monkeypatch.setattr(sys, "argv", ["build_exe.py"])
    calls = []
    monkeypatch.setattr(build_exe.subprocess, "run", lambda *args, **kwargs: (calls.append(kwargs), SimpleNamespace(returncode=0))[1])
    build_exe.main()
    search_path = calls[0]["env"]["PATH"].split(os.pathsep)
    assert foreign_path not in search_path
    assert str(Path(sys.executable).parent) in search_path
    assert str(Path(os.environ["SYSTEMROOT"]) / "System32") in search_path
    assert os.environ["PATH"] == foreign_path
    assert (out / "ffmpeg" / "ffmpeg.exe").read_bytes() == b"test binary"
    assert {d: (out / d).read_text(encoding="utf-8") for d in ("README.md", "LEEME.md", "CHANGELOG.md")} == {d: "test document" for d in ("README.md", "LEEME.md", "CHANGELOG.md")}


def test_portable_build_bundles_sidecar_sources_without_local_bytecode():
    """sidecar/__pycache__ .pyc files embed the builder's absolute paths (Windows user name, project folder)."""
    from pathlib import Path

    spec = (Path(__file__).parent.parent / "marea.spec").read_text(encoding="utf-8")
    assert '("sidecar", "sidecar")' not in spec  # the whole folder would bring its __pycache__ along
    assert 'f.suffix in (".py", ".txt")' in spec
