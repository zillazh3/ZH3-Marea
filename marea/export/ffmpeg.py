"""Codificación con ffmpeg: frames RGBA crudos por pipe -> H.264 (+ AAC), sin escribir PNGs a disco."""
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # que ffmpeg no abra una consola en Windows


def find_ffmpeg(name: str = "ffmpeg") -> str:
    """MAREA_FFMPEG > carpeta ffmpeg/ junto al .exe (versión empaquetada) > PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    if name == "ffmpeg" and os.environ.get("MAREA_FFMPEG"):
        return os.environ["MAREA_FFMPEG"]
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent / "ffmpeg" / exe
        if bundled.exists():
            return str(bundled)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError("No encontré ffmpeg. Instálalo o ponlo en la carpeta ffmpeg/ junto a la app.")
    return found


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """NVENC aparece en la lista de encoders aunque el driver no lo permita: se prueba codificando un frame."""
    try:
        r = subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=black:s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True, timeout=20, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def build_command(out: Path, w: int, h: int, fps: int, encoder: str = "cpu", beep: bool = False,
                  audio: Path | None = None) -> list[str]:
    """Orden de ffmpeg. `beep`: pista con un pitido de 120 ms al inicio y silencio después (marcador de sincronía)."""
    cmd = [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    audio_args = []
    if beep:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=0.12"]
        audio_args = ["-filter_complex", "[1:a]volume=0.8,apad[a]", "-map", "0:v", "-map", "[a]", "-shortest"]
    elif audio:
        cmd += ["-i", str(audio)]
        audio_args = ["-map", "0:v", "-map", "1:a", "-shortest"]
    # RGB -> YUV con la matriz BT.709 (lo que los reproductores asumen en HD) y etiquetado explícito
    cmd += ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"]
    if encoder == "nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "16", "-b:v", "0"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "16"]
    cmd += ["-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-color_range", "tv", "-movflags", "+faststart"]
    if audio_args:
        cmd += audio_args + ["-c:a", "aac", "-b:a", "320k"]
    return cmd + [str(out)]


class FfmpegWriter:
    """Context manager: `write(frame)` con arrays (alto, ancho, 4) uint8 RGBA."""

    def __init__(self, out: Path, w: int, h: int, fps: int, encoder: str = "cpu", beep: bool = False,
                 audio: Path | None = None):
        self.out, self.cmd = Path(out), build_command(out, w, h, fps, encoder, beep, audio)
        self.proc = None

    def __enter__(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        return self

    def write(self, frame):
        try:
            self.proc.stdin.write(memoryview(frame))  # sin copia: el array ya es contiguo
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(self._error() or "ffmpeg se cerró de forma inesperada.") from e

    def _error(self) -> str:
        try:
            return self.proc.stderr.read().decode(errors="replace").strip()
        except Exception:
            return ""

    def __exit__(self, exc_type, exc, tb):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if exc_type:  # error o cancelación: no dejar un mp4 a medias
            self.proc.kill()
            self.proc.wait()
            self._discard()
            return False
        err = self.proc.stderr.read().decode(errors="replace").strip()
        if self.proc.wait() != 0:
            self._discard()
            raise RuntimeError(f"ffmpeg no pudo codificar el video: {err}")
        return False

    def _discard(self):
        if self.out.is_file():  # nunca tocar otra cosa (p. ej. si el destino resultó ser una carpeta)
            self.out.unlink()
