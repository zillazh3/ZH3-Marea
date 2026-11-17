"""Codificación con ffmpeg: frames RGBA crudos por pipe -> H.264 / HEVC / AV1 (+ AAC), sin escribir PNGs a disco."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import threading

from marea.i18n import t
from marea.i18n import Labels
from marea.sysutil import NO_WINDOW, find_ffmpeg

# destino -> (extensión del archivo; "" = carpeta, etiqueta en la UI). Los tres últimos llevan canal alfa.
# (the label is an i18n key; the Mapping resolves it with t() on every read, so callers keep using `TARGETS[k][1]`)
TARGETS = Labels({"mp4": (".mp4", "export.target.mp4"), "webm": (".webm", "export.target.webm"),
                  "prores": (".mov", "export.target.prores"), "png": ("", "export.target.png")}, lambda v: (v[0], t(v[1])))
ALPHA_TARGETS = ("webm", "prores", "png")


# Códec del video del MP4 y quién lo codifica. H.264 es el predeterminado: lo acepta todo.
CODECS = {"h264": "H.264", "hevc": "HEVC (H.265)", "av1": "AV1"}
BACKENDS = {"cpu": "CPU", "nvenc": "NVIDIA NVENC", "amf": "AMD AMF", "qsv": "Intel Quick Sync (QSV)"}
HW_ORDER = ("nvenc", "amf", "qsv")  # preferencia de «Automático»
# (backend, códec) -> (encoder de ffmpeg, pix_fmt, calidad). QSV pide nv12; el resto, yuv420p.
_ENCODERS = {
    ("cpu", "h264"): ("libx264", "yuv420p", ["-preset", "slow", "-crf", "16"]),
    ("cpu", "hevc"): ("libx265", "yuv420p", ["-preset", "medium", "-crf", "18", "-tag:v", "hvc1", "-x265-params", "log-level=error"]),
    ("cpu", "av1"): ("libsvtav1", "yuv420p", ["-preset", "6", "-crf", "28"]),
    ("nvenc", "h264"): ("h264_nvenc", "yuv420p", ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "16", "-b:v", "0"]),
    ("nvenc", "hevc"): ("hevc_nvenc", "yuv420p", ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "18", "-b:v", "0", "-tag:v", "hvc1"]),
    ("nvenc", "av1"): ("av1_nvenc", "yuv420p", ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "24", "-b:v", "0"]),
    ("amf", "h264"): ("h264_amf", "yuv420p", ["-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"]),
    ("amf", "hevc"): ("hevc_amf", "yuv420p", ["-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "20", "-tag:v", "hvc1"]),
    ("amf", "av1"): ("av1_amf", "yuv420p", ["-quality", "quality", "-rc", "cqp", "-qp_i", "26", "-qp_p", "26"]),
    ("qsv", "h264"): ("h264_qsv", "nv12", ["-preset", "slower", "-global_quality", "18"]),
    ("qsv", "hevc"): ("hevc_qsv", "nv12", ["-preset", "slower", "-global_quality", "20", "-tag:v", "hvc1"]),
    ("qsv", "av1"): ("av1_qsv", "nv12", ["-preset", "slower", "-global_quality", "26"]),
}
_detected: dict[str, tuple[str, ...]] | None = None
_detect_lock = threading.Lock()


def video_args(backend: str, codec: str) -> tuple[str, list[str]]:
    """(pix_fmt, argumentos `-c:v …`) de un backend y códec. ValueError si esa combinación no existe."""
    try:
        name, pix, quality = _ENCODERS[backend, codec]
    except KeyError:
        raise ValueError(f"Codificador desconocido: {backend}/{codec}") from None
    return pix, ["-c:v", name, *quality]


def _probe(backend: str, codec: str) -> bool:
    """Codifica unos frames de prueba con los mismos argumentos del export: si funciona aquí, funcionará allá."""
    pix, args = video_args(backend, codec)
    try:
        r = subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=black:s=256x256:r=30:d=0.2", "-vf", f"format={pix}", *args, "-pix_fmt", pix,
                            "-f", "null", "-"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _listed_encoders() -> str:
    try:
        r = subprocess.run([find_ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, timeout=20, creationflags=NO_WINDOW)
        return r.stdout.decode(errors="replace")
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def detect_encoders() -> dict[str, tuple[str, ...]]:
    """backend -> códecs que esta máquina codifica de verdad (ffmpeg lista encoders aunque el driver o la GPU no los permitan,
    así que cada uno se prueba). Bloquea unos segundos la primera vez; después responde de la caché."""
    global _detected
    with _detect_lock:
        if _detected is None:
            listing = _listed_encoders()
            pairs = {b: [c for (bb, c), spec in _ENCODERS.items() if bb == b and f" {spec[0]} " in listing] for b in BACKENDS}

            def one_backend(b: str) -> tuple[str, ...]:  # un hilo por backend: las pruebas de una misma GPU van en serie
                return tuple(c for c in pairs[b] if _probe(b, c))

            with ThreadPoolExecutor(max_workers=len(BACKENDS)) as ex:
                _detected = dict(zip(BACKENDS, ex.map(one_backend, BACKENDS)))
        return _detected


def detected_encoders() -> dict[str, tuple[str, ...]] | None:
    """La detección si ya terminó, None si no (para no bloquear la interfaz)."""
    return _detected


def resolve_encoder(backend: str, codec: str) -> tuple[str, str]:
    """(backend, códec) concretos para un export. «auto» = la primera GPU detectada que codifique `codec`, si no la CPU.
    Una GPU que no sabe ese códec cae a la CPU; un códec que nadie codifica cae a H.264 en CPU."""
    found = detect_encoders()
    codec = codec if codec in CODECS else "h264"
    for b in HW_ORDER if backend == "auto" else (backend,):
        if codec in found.get(b, ()):
            return b, codec
    return ("cpu", codec) if codec in found.get("cpu", ()) else ("cpu", "h264")


def nvenc_available() -> bool:
    return "h264" in detect_encoders()["nvenc"]


@lru_cache(maxsize=1)
def alpha_encoders() -> dict[str, bool]:
    """Qué destinos con alfa puede codificar este ffmpeg: se prueba un frame transparente real (como `nvenc_available`)."""
    specs = {"webm": ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p"], "prores": ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]}
    found = {}
    for name, args in specs.items():
        try:
            r = subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=c=black@0.0:s=64x64:d=0.1,format=rgba", *args, "-f", "null", "-"],
                               capture_output=True, timeout=30, creationflags=NO_WINDOW)
            found[name] = r.returncode == 0
        except (FileNotFoundError, subprocess.SubprocessError):
            found[name] = False
    return found


def default_encoder() -> str:
    """El backend de «Automático» para H.264: la primera GPU detectada (NVIDIA, AMD, Intel) o, si no hay, la CPU."""
    return resolve_encoder("auto", "h264")[0]


def png_pattern(folder: Path) -> Path:
    return folder / f"{folder.name[:80]}_%05d.png"


def build_command(out: Path, w: int, h: int, fps: int, encoder: str = "cpu", beep: bool = False,
                  audio: Path | None = None, target: str = "mp4", codec: str = "h264") -> list[str]:
    """Orden de ffmpeg. `beep`: pista con un pitido de 120 ms al inicio y silencio después (marcador de sincronía).
    Los frames llegan como RGBA *sin premultiplicar* (alfa recto): es lo que esperan VP9/ProRes/PNG."""
    cmd = [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if target == "png":  # `out` es la carpeta; sin audio
        return cmd + ["-c:v", "png", "-pix_fmt", "rgba", "-f", "image2", str(png_pattern(out))]
    audio_args = []
    if beep:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=0.12"]
        audio_args = ["-filter_complex", "[1:a]volume=0.8,apad[a]", "-map", "0:v", "-map", "[a]", "-shortest"]
    elif audio:
        cmd += ["-i", str(audio)]
        audio_args = ["-map", "0:v", "-map", "1:a", "-shortest"]
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    if target in ("webm", "prores"):
        if target == "webm":
            pix, vcodec = "yuva420p", ["-c:v", "libvpx-vp9", "-crf", "18", "-b:v", "0", "-row-mt", "1", "-auto-alt-ref", "0"]
            acodec = ["-c:a", "libopus", "-b:a", "192k"]
        else:
            pix, vcodec = "yuva444p10le", ["-c:v", "prores_ks", "-profile:v", "4444", "-alpha_bits", "16", "-vendor", "apl0"]
            acodec = ["-c:a", "pcm_s16le"]
        cmd += ["-vf", f"scale=out_color_matrix=bt709:out_range=tv,format={pix}"] + vcodec + ["-pix_fmt", pix] + tags
        if audio_args:
            cmd += audio_args + acodec
        return cmd + [str(out)]
    # RGB -> YUV con la matriz BT.709 (lo que los reproductores asumen en HD) y etiquetado explícito
    pix, vcodec = video_args(encoder, codec)
    cmd += ["-vf", f"scale=out_color_matrix=bt709:out_range=tv,format={pix}", *vcodec, "-pix_fmt", pix, *tags,
            "-movflags", "+faststart"]
    if audio_args:
        cmd += audio_args + ["-c:a", "aac", "-b:a", "320k"]
    return cmd + [str(out)]


class FfmpegWriter:
    """Context manager: `write(frame)` con arrays (alto, ancho, 4) uint8 RGBA."""

    def __init__(self, out: Path, w: int, h: int, fps: int, encoder: str = "cpu", beep: bool = False,
                 audio: Path | None = None, target: str = "mp4", codec: str = "h264"):
        self.out, self.target = Path(out), target
        self.cmd = build_command(self.out, w, h, fps, encoder, beep, audio, target, codec)
        self.proc = None
        self._err_chunks: deque[bytes] = deque(maxlen=64)
        self._err_thread: threading.Thread | None = None

    def __enter__(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        try:  # creación exclusiva: `-y` solo pisa este archivo propio y `_discard` nunca borra el de otro export
            if self.target == "png":
                self.out.mkdir()  # carpeta propia y nueva (falla si ya existe)
            else:
                open(self.out, "xb").close()
        except OSError as e:
            raise RuntimeError(t("export.writeFailed", name=self.out.name)) from e
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
            raise RuntimeError(self._error() or t("export.ffmpegClosed")) from e

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
            raise RuntimeError(t("export.encodeFailed", error=err))
        return False

    def _discard(self):
        if self.target == "png":
            shutil.rmtree(self.out, ignore_errors=True)  # la carpeta la creó este export: solo contiene sus frames
        elif self.out.is_file():  # nunca tocar otra cosa (p. ej. si el destino resultó ser una carpeta)
            self.out.unlink()
