"""Versioned portable archive layout without compiling the application."""
import zipfile

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
