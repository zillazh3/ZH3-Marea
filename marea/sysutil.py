"""Shared process, ffmpeg, and safe-filename utilities without UI or Qt dependencies."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from marea.i18n import t

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # prevent console flash on Windows

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BS = "\\"
_EXTENDED = _BS * 2 + "?" + _BS


def is_local_path(p) -> bool:
    """Allowlist: relative paths, drive paths (C:\\...) and extended drive paths (\\\\?\\C:\\...). Anything else that starts with a
    backslash (UNC, \\\\?\\UNC, \\\\?\\GLOBALROOT, \\\\.\\, \\??\\) can reach a network share, and touching it on open would send NTLM credentials."""
    s = os.fspath(p).replace("/", _BS)
    if s.startswith(_EXTENDED):
        return len(s) >= 6 and s[4].isalpha() and s[5] == ":"
    return not s.startswith(_BS)


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{d}{i}" for d in ("COM", "LPT") for i in range(1, 10))}


def find_ffmpeg(name: str = "ffmpeg") -> str:
    """Resolve `ffmpeg`/`ffprobe` binary: MAREA_FFMPEG > bundled/ffmpeg > PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    if name == "ffmpeg" and os.environ.get("MAREA_FFMPEG"):
        return os.environ["MAREA_FFMPEG"]
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent / "ffmpeg" / exe
        if bundled.exists():
            return str(bundled)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(t("errors.sysutil.noFfmpeg"))
    return found


def run_ffmpeg(args: list[str], *, timeout: int = 30, name: str = "ffmpeg", **kw) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe command hidden (no console window) with captured output."""
    return subprocess.run(
        [find_ffmpeg(name), *args],
        capture_output=True,
        creationflags=NO_WINDOW,
        timeout=timeout,
        **kw,
    )


def kill_tree(pid: int) -> None:
    """Terminate a process and all its descendants."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        os.kill(pid, 9)


def safe_filename(text: str, fallback: str, limit: int = 100) -> str:
    """Return a valid Windows filename without path separators, reserved names, or empty output."""
    name = _INVALID.sub("", text).strip(" .")[:limit].rstrip(" .") or fallback
    return "_" + name if name.split(".")[0].rstrip().upper() in _RESERVED else name


def unique_path(folder: Path, name: str, taken=()) -> Path:
    """Return `folder/name`, or `folder/stem (n)suffix` if it already exists or is in `taken`."""
    p, stem, suffix, n = folder / name, Path(name).stem, Path(name).suffix, 2
    while p.exists() or p in taken:
        p = folder / f"{stem} ({n}){suffix}"
        n += 1
    return p
