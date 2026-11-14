"""Codificación con ffmpeg: frames RGBA crudos por pipe -> H.264 (+ AAC), sin escribir PNGs a disco."""
from collections import deque
from functools import lru_cache
from pathlib import Path
import subprocess
import threading

from marea.sysutil import NO_WINDOW, find_ffmpeg


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


def default_encoder() -> str:
    """Prefer hardware NVENC encoding when available on the host GPU; otherwise fall back to CPU libx264."""
    return "nvenc" if nvenc_available() else "cpu"


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
        self._err_chunks: deque[bytes] = deque(maxlen=64)
        self._err_thread: threading.Thread | None = None

    def __enter__(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        try:  # creación exclusiva: `-y` solo pisa este archivo propio y `_discard` nunca borra el de otro export
            open(self.out, "xb").close()
        except OSError as e:
            raise RuntimeError(f"No puedo escribir «{self.out.name}»: ya existe o la carpeta no lo permite.") from e
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=16 * 1024 * 1024, creationflags=NO_WINDOW,
            )
            self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self._err_thread.start()
        except BaseException:
            self._discard()
            raise
        return self

    def _drain_stderr(self):
        pipe = self.proc.stderr if self.proc else None
        if pipe is None:
            return
        try:
            while chunk := pipe.read(4096):
                self._err_chunks.append(chunk)
        except OSError:
            pass

    def write(self, frame):
        try:
            self.proc.stdin.write(memoryview(frame))  # sin copia: el array ya es contiguo
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(self._error() or "ffmpeg se cerró de forma inesperada.") from e

    def abort(self):
        """Forcefully terminate ffmpeg so a blocked pipe write unblocks on cancellation."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _error(self) -> str:
        if self.proc:
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.SubprocessError:
                pass
        if self._err_thread:
            self._err_thread.join(timeout=1.0)
        return b"".join(self._err_chunks).decode(errors="replace").strip()

    def __exit__(self, exc_type, exc, tb):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if exc_type:  # error o cancelación: no dejar un mp4 a medias
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            if self._err_thread:
                self._err_thread.join(timeout=1.0)
            self._discard()
            return False
        code = self.proc.wait()
        err = self._error()
        if code != 0:
            self._discard()
            raise RuntimeError(f"ffmpeg no pudo codificar el video: {err}")
        return False

    def _discard(self):
        if self.out.is_file():  # nunca tocar otra cosa (p. ej. si el destino resultó ser una carpeta)
            self.out.unlink()
