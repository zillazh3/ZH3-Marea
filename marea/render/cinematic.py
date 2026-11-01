"""Decodificación determinista de video con ffmpeg (-an) y composición Skia del fondo cinemático.

- Sin `ffprobe` ni `QtMultimedia`: compatible 100 % con la carpeta portable (`ffmpeg/ffmpeg.exe`).
- El audio del video se descarta siempre (`-an`); nunca llega al motor ni al export.
- Mismo índice de frame (`floor(t_src · fps_num / fps_den)`) y mismo bloque alineado (`CHUNK_FRAMES`) en preview y export.
"""
from __future__ import annotations

import math
import re
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

import numpy as np
import skia

from marea.engine.curves import clamp01
from marea.export.ffmpeg import NO_WINDOW, find_ffmpeg
from marea.model.cinematic import ActiveInterval, CinematicConfig, CinematicTreatment, VideoResource, evaluate_at

CHUNK_FRAMES = 12       # ~0.4–0.5 s por bloque: rápido de decodificar y ligero en RAM
MAX_CACHE_FRAMES = 28   # techo de memoria (~230 MB máx en 1080×1920, ~58 MB en 540×960, ~14 MB en 270×480)
LOOKAHEAD_MS = 900.0    # pre-carga antes de entrar a un intervalo durante la reproducción en vivo
GRAIN_TILE = 128        # mosaico determinista de grano de película (se repite con shader en 24 Hz)


class VideoError(ValueError):
    pass


# ---------------- inspección de metadatos con ffmpeg.exe ----------------

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_RES_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_SAR_RE = re.compile(r"\bSAR\s+(\d+):(\d+)\b")
_FPS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*fps\b")
_TBR_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*tbr\b")


def _hms_to_ms(h: str, m: str, s: str) -> int:
    return max(0, round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000.0))


def _fps_to_rational(fps: float) -> tuple[int, int]:
    """Convierte fps decimal de ffmpeg a racional exacto (incluyendo NTSC 23.976 / 29.97 / 59.94), acotado a [1, 60]."""
    fps = max(1.0, min(60.0, float(fps)))
    for ntsc, num, den in ((23.976, 24000, 1001), (29.97, 30000, 1001), (59.94, 60000, 1001)):
        if abs(fps - ntsc) < 0.02:
            return num, den
    rounded = round(fps)
    if abs(fps - rounded) < 0.005:
        return max(1, int(rounded)), 1
    return max(1, round(fps * 1000)), 1000


def probe_video(path: str | Path, resource_id: str = "vid_1") -> VideoResource:
    """Inspecciona duración, resolución y FPS usando solo `ffmpeg`, y verifica que al menos 1 frame sea decodificable."""
    p = Path(path)
    if not p.is_file():
        raise VideoError("No encontré ese archivo de video.")
    try:
        ffmpeg_bin = find_ffmpeg()
    except FileNotFoundError as e:
        raise VideoError(str(e)) from e

    try:
        info = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(p)],
            capture_output=True,
            creationflags=NO_WINDOW,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise VideoError("El archivo de video tardó demasiado en abrirse.") from e
    except OSError as e:
        raise VideoError(f"No pude ejecutar ffmpeg: {e}") from e

    stderr = info.stderr.decode(errors="replace")
    video_line = ""
    for line in stderr.splitlines():
        if "Stream #" in line and "Video:" in line and "(attached pic)" not in line.lower():
            video_line = line
            break
    if not video_line:
        raise VideoError("Ese archivo no contiene una pista de video válida. Usa un MP4, MOV, MKV o WebM.")

    res_m = _RES_RE.search(video_line)
    if not res_m:
        raise VideoError("No pude leer la resolución del video.")
    width, height = int(res_m.group(1)), int(res_m.group(2))
    if width <= 0 or height <= 0:
        raise VideoError("La resolución del video no es válida.")

    sar_m = _SAR_RE.search(video_line)
    sar_num = max(1, int(sar_m.group(1))) if sar_m else 1
    sar_den = max(1, int(sar_m.group(2))) if sar_m else 1

    fps_m = _FPS_RE.search(video_line) or _TBR_RE.search(video_line)
    fps_val = float(fps_m.group(1)) if fps_m else 30.0
    fps_num, fps_den = _fps_to_rational(fps_val)

    dur_m = _DUR_RE.search(stderr)
    duration_ms = _hms_to_ms(*dur_m.groups()) if dur_m else 0
    if duration_ms <= 0:
        # Contenedores sin duración en cabecera (p. ej. ciertos WebM/MKV): remux rápido a null para medir el último PTS
        try:
            remux = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-i", str(p), "-an", "-c:v", "copy", "-f", "null", "-"],
                capture_output=True,
                creationflags=NO_WINDOW,
                timeout=30,
            )
            times = _TIME_RE.findall(remux.stderr.decode(errors="replace"))
            if times:
                duration_ms = _hms_to_ms(*times[-1])
        except subprocess.SubprocessError:
            duration_ms = 0

    # Verificación real de decodificación (descarta archivos truncados, codecs corruptos o contenedores sin frames)
    try:
        chk = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-i", str(p), "-an", "-frames:v", "1",
             "-vf", "scale=16:16", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
            capture_output=True,
            creationflags=NO_WINDOW,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise VideoError("El video tardó demasiado en decodificar un fotograma de prueba.") from e
    if chk.returncode != 0 or len(chk.stdout) < 16 * 16 * 4:
        raise VideoError("No pude decodificar fotogramas de ese video. Comprueba que el archivo no esté dañado.")

    if duration_ms <= 0:
        duration_ms = max(1, round(1000.0 * fps_den / fps_num))

    v_start, v_end, cuts_ms, peaks_ms = _probe_visual_profile(ffmpeg_bin, p, duration_ms)

    return VideoResource(
        id=resource_id,
        path=str(p.resolve()),
        duration_ms=duration_ms,
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        sar_num=sar_num,
        sar_den=sar_den,
        visual_start_ms=v_start,
        visual_end_ms=v_end,
        scene_cuts_ms=cuts_ms,
        salience_peaks_ms=peaks_ms,
    )


def _probe_visual_profile(
    ffmpeg_bin: str,
    path: Path,
    duration_ms: int,
) -> tuple[int, int, list[int], list[int]]:
    """Extrae en <0.3 s luminancia y movimiento a 32×18 @ 4 fps (-an) para evitar cuadros negros y detectar planos."""
    if duration_ms <= 900:
        return 0, max(0, int(duration_ms)), [], []
    step_ms = 250.0  # 4 fps
    frame_bytes = 32 * 18
    try:
        scan = subprocess.run(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-i", str(path), "-an",
                "-vf", "fps=4:start_time=0:round=down,scale=32:18:flags=fast_bilinear",
                "-frames:v", "3600", "-f", "rawvideo", "-pix_fmt", "gray", "-",
            ],
            capture_output=True,
            creationflags=NO_WINDOW,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, int(duration_ms), [], []

    n = len(scan.stdout) // frame_bytes
    if n < 3:
        return 0, int(duration_ms), [], []

    frames = np.frombuffer(scan.stdout[: n * frame_bytes], dtype=np.uint8).reshape((n, 18, 32)).astype(np.float32)
    luma_mean = frames.mean(axis=(1, 2))
    luma_p90 = np.percentile(frames, 90, axis=(1, 2))
    spatial_std = frames.std(axis=(1, 2))
    motion = np.zeros(n, dtype=np.float32)
    motion[1:] = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2))

    visible = (luma_p90 >= 20.0) | (luma_mean >= 11.0)
    if bool(visible.any()):
        first_idx = int(np.argmax(visible))
        last_idx = int(n - 1 - np.argmax(visible[::-1]))
        v_start = int(round(first_idx * step_ms)) if first_idx > 0 else 0
        v_end = int(duration_ms) if last_idx >= n - 2 else min(int(duration_ms), int(round((last_idx + 1) * step_ms)))
        if v_end <= v_start + 500:
            v_start, v_end = 0, int(duration_ms)
    else:
        v_start, v_end = 0, int(duration_ms)

    # Cambios de plano (picos bruscos de diferencia entre cuadros seguidos de imagen visible)
    cuts_ranked: list[tuple[float, int]] = []
    last_cut_k = -3
    for k in range(1, n):
        if not visible[k] or (k + 1 < n and not visible[k + 1]):
            continue
        w0, w1 = max(1, k - 6), min(n, k + 7)
        local_med = float(np.median(motion[w0:w1]))
        mv = float(motion[k])
        is_peak = mv >= motion[k - 1] and (k + 1 == n or mv >= motion[k + 1])
        if is_peak and mv >= max(13.0, 2.6 * local_med + 4.0) and (k - last_cut_k) >= 3:
            t_cut = int(round(k * step_ms))
            if v_start <= t_cut < v_end:
                cuts_ranked.append((mv, t_cut))
                last_cut_k = k
    cuts_ranked.sort(reverse=True)
    cuts_ms = sorted(t for _, t in cuts_ranked[:64])

    # Picos de prominencia visual (alto contraste espacial + movimiento activo) para no partirlos al inicio
    salience = spatial_std * (0.4 + 0.6 * np.minimum(motion, 25.0) / 25.0)
    peaks_ranked: list[tuple[float, int]] = []
    if float(salience.max()) > 5.0:
        sal_thr = float(np.percentile(salience, 82))
        last_pk = -6
        for k in range(1, n - 1):
            sv = float(salience[k])
            if visible[k] and sv >= sal_thr and sv >= salience[k - 1] and sv >= salience[k + 1] and (k - last_pk) >= 6:
                t_pk = int(round(k * step_ms))
                if v_start <= t_pk < v_end:
                    peaks_ranked.append((sv, t_pk))
                    last_pk = k
    peaks_ranked.sort(reverse=True)
    peaks_ms = sorted(t for _, t in peaks_ranked[:32])
    return v_start, v_end, cuts_ms, peaks_ms


# ---------------- decodificador determinista por bloques + caché LRU ----------------

class VideoDecoder:
    """Entrega frames `skia.Image` escalados y recortados a `(w, h)` para cualquier timestamp del video fuente."""

    def __init__(
        self,
        resource: VideoResource,
        size: tuple[int, int],
        async_decode: bool = False,
        on_frame_ready: Callable[[], None] | None = None,
    ):
        self.resource = resource
        self.w, self.h = max(2, int(size[0])), max(2, int(size[1]))
        self.async_decode = async_decode
        self.on_frame_ready = on_frame_ready
        self.fps_num = max(1, int(resource.fps_num))
        self.fps_den = max(1, int(resource.fps_den))
        self.max_frames = max(1, resource.frame_count)
        self._frame_bytes = self.w * self.h * 4
        self._vf = (
            f"fps={self.fps_num}/{self.fps_den}:start_time=0:round=down,"
            f"scale=trunc(iw*sar+0.5):ih,"
            f"scale={self.w}:{self.h}:force_original_aspect_ratio=increase:flags=bicubic,"
            f"crop={self.w}:{self.h},setsar=1"
        )

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._raw_cache: OrderedDict[int, bytes] = OrderedDict()  # frame_idx -> RGBA bytes (seguro entre hilos)
        self._img_cache: OrderedDict[int, skia.Image] = OrderedDict()  # solo hilo de render
        self._inflight: set[int] = set()
        self._queue: list[int] = []  # pila LIFO/prioritaria de chunk_ids pendientes
        self._failed_chunks: set[int] = set()
        self._broken = False  # archivo borrado o ffmpeg inoperante: cae al fondo de portada sin bloquear
        self._closed = False
        self._active_proc: subprocess.Popen | None = None
        self._last_tail_raw: bytes | None = None

        self._worker: threading.Thread | None = None
        if self.async_decode:
            self._worker = threading.Thread(target=self._worker_loop, name="marea-video-prefetch", daemon=True)
            self._worker.start()

    def frame_index(self, src_ms: float) -> int:
        s = max(0.0, float(src_ms)) / 1000.0
        idx = int(math.floor(s * self.fps_num / self.fps_den + 1e-6))
        return max(0, min(self.max_frames - 1, idx))

    def prefetch_ms(self, src_ms: float, urgent: bool = False):
        if not self.async_decode or self._broken:
            return
        idx = self.frame_index(src_ms)
        self._enqueue_chunk(idx // CHUNK_FRAMES, urgent=urgent)

    def get_image(self, src_ms: float) -> skia.Image | None:
        """Obtiene el `skia.Image` exacto para `src_ms` (solo llamar desde el hilo que dibuja en Skia)."""
        if self._broken:
            return None
        idx = self.frame_index(src_ms)
        if idx in self._img_cache:
            self._img_cache.move_to_end(idx)
            if self.async_decode and (idx % CHUNK_FRAMES) >= (CHUNK_FRAMES // 2):
                next_idx = idx + CHUNK_FRAMES
                if next_idx < self.max_frames:
                    self._enqueue_chunk(next_idx // CHUNK_FRAMES, urgent=False)
            return self._img_cache[idx]

        raw, actual_idx = self._get_raw(idx)
        if raw is None:
            return None
        if actual_idx in self._img_cache:
            self._img_cache.move_to_end(actual_idx)
            return self._img_cache[actual_idx]
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((self.h, self.w, 4))
        img = skia.Image.fromarray(arr, colorType=skia.kRGBA_8888_ColorType, copy=True)
        self._img_cache[actual_idx] = img
        while len(self._img_cache) > MAX_CACHE_FRAMES:
            self._img_cache.popitem(last=False)
        if self.async_decode and actual_idx == idx and (idx % CHUNK_FRAMES) >= (CHUNK_FRAMES // 2):
            next_idx = idx + CHUNK_FRAMES
            if next_idx < self.max_frames:
                self._enqueue_chunk(next_idx // CHUNK_FRAMES, urgent=False)
        return img

    def _get_raw(self, idx: int) -> tuple[bytes | None, int]:
        chunk_id = idx // CHUNK_FRAMES
        with self._lock:
            raw = self._raw_cache.get(idx)
            if raw is not None:
                self._raw_cache.move_to_end(idx)
                return raw, idx
            if self._broken or chunk_id in self._failed_chunks:
                return None, idx

        if not self.async_decode:
            self._decode_and_store(chunk_id)
            with self._lock:
                raw = self._raw_cache.get(idx)
                if raw is not None:
                    self._raw_cache.move_to_end(idx)
                return raw, idx

        # Modo asíncrono (UI): nunca bloquea el hilo principal; pide el bloque con prioridad y devuelve el frame más cercano si existe
        self._enqueue_chunk(chunk_id, urgent=True)
        with self._lock:
            if not self._raw_cache:
                return None, idx
            nearest = min(self._raw_cache, key=lambda k: abs(k - idx))
            if abs(nearest - idx) <= CHUNK_FRAMES * 2:
                return self._raw_cache[nearest], nearest
            return None, idx

    def _enqueue_chunk(self, chunk_id: int, urgent: bool = False):
        with self._cond:
            if self._closed or self._broken or chunk_id in self._failed_chunks or chunk_id in self._inflight:
                return
            first_idx = chunk_id * CHUNK_FRAMES
            if first_idx in self._raw_cache:
                return
            if chunk_id in self._queue:
                if urgent:
                    self._queue.remove(chunk_id)
                    self._queue.insert(0, chunk_id)
                return
            if urgent:
                self._queue.insert(0, chunk_id)
                if len(self._queue) > 6:
                    self._queue = self._queue[:6]
            else:
                if len(self._queue) < 6:
                    self._queue.append(chunk_id)
            self._cond.notify()

    def _worker_loop(self):
        while True:
            with self._cond:
                while not self._closed and not self._queue:
                    self._cond.wait()
                if self._closed:
                    return
                chunk_id = self._queue.pop(0)
                self._inflight.add(chunk_id)
            ok = self._decode_and_store(chunk_id)
            with self._cond:
                self._inflight.discard(chunk_id)
                self._cond.notify_all()
            if ok and not self._closed and self.on_frame_ready is not None:
                try:
                    self.on_frame_ready()
                except RuntimeError:
                    pass  # el widget de Qt ya se destruyó

    def wait_idle(self, timeout: float = 5.0):
        """Espera a que terminen los bloques en cola (útil en tests de paridad preview/export)."""
        if not self.async_decode:
            return
        with self._cond:
            self._cond.wait_for(lambda: self._closed or (not self._queue and not self._inflight), timeout=timeout)

    def _run_ffmpeg_raw(self, t0_s: float, count: int) -> bytes | None:
        p = Path(self.resource.path)
        if not p.is_file():
            self._broken = True
            return None
        try:
            ffmpeg_bin = find_ffmpeg()
        except FileNotFoundError:
            self._broken = True
            return None

        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, t0_s):.6f}",
            "-i", str(p),
            "-an",
            "-vf", self._vf,
            "-frames:v", str(count),
            "-f", "rawvideo", "-pix_fmt", "rgba", "-",
        ]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            with self._lock:
                if self._closed:
                    proc.kill()
                    proc.wait()
                    return None
                self._active_proc = proc
            out, _ = proc.communicate(timeout=30)
            if proc.returncode != 0 and not out:
                return None
            return out
        except (OSError, subprocess.SubprocessError):
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait()
                except OSError:
                    pass
            return None
        finally:
            with self._lock:
                if self._active_proc is proc:
                    self._active_proc = None

    def _decode_and_store(self, chunk_id: int) -> bool:
        k0 = chunk_id * CHUNK_FRAMES
        t0 = k0 * self.fps_den / self.fps_num
        need = min(CHUNK_FRAMES, max(1, self.max_frames - k0))
        out = self._run_ffmpeg_raw(t0, need)
        frames: list[bytes] = []
        if out:
            n_got = len(out) // self._frame_bytes
            frames = [out[i * self._frame_bytes:(i + 1) * self._frame_bytes] for i in range(n_got)]

        # Si el contenedor reportó unos ms de más al final y `t0` cayó justo tras el último PTS, sostenemos el último frame válido
        if not frames and chunk_id > 0 and not self._broken and not self._closed:
            if self._last_tail_raw is not None:
                frames = [self._last_tail_raw]
            else:
                back_s = max(0.0, self.resource.duration_ms / 1000.0 - max(0.25, 2.0 * self.fps_den / self.fps_num))
                tail = self._run_ffmpeg_raw(back_s, 1)
                if tail and len(tail) >= self._frame_bytes:
                    self._last_tail_raw = tail[:self._frame_bytes]
                    frames = [self._last_tail_raw]

        with self._lock:
            if self._closed:
                return False
            if not frames:
                if chunk_id == 0:
                    self._broken = True
                else:
                    self._failed_chunks.add(chunk_id)
                return False
            self._last_tail_raw = frames[-1]
            while len(frames) < need:
                frames.append(frames[-1])
            for offset, raw in enumerate(frames):
                f_idx = k0 + offset
                if f_idx in self._raw_cache:
                    self._raw_cache.move_to_end(f_idx)
                else:
                    self._raw_cache[f_idx] = raw
            while len(self._raw_cache) > MAX_CACHE_FRAMES:
                self._raw_cache.popitem(last=False)
            return True

    def close(self):
        with self._cond:
            self._closed = True
            self._queue.clear()
            proc = self._active_proc
            self._cond.notify_all()
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        with self._lock:
            self._raw_cache.clear()
            self._img_cache.clear()


# ---------------- estética cinemática de recuerdo (Skia) ----------------

def _color_matrix(saturation: float, contrast: float) -> skia.ColorFilter:
    """Matriz combinada de saturación (BT.709) y contraste suave alrededor de gris medio 0.5."""
    r, g, b = 0.2126, 0.7152, 0.0722
    i = 1.0 - saturation
    c = contrast
    t = 0.5 * (1.0 - c)
    return skia.ColorFilters.Matrix([
        c * (r * i + saturation), c * (g * i), c * (b * i), 0.0, t,
        c * (r * i), c * (g * i + saturation), c * (b * i), 0.0, t,
        c * (r * i), c * (g * i), c * (b * i + saturation), 0.0, t,
        0.0, 0.0, 0.0, 1.0, 0.0,
    ])


def _make_grain_tile(side: int = GRAIN_TILE) -> skia.Image:
    """Mosaico gaussiano monocromo fijo (semilla 2026): grano orgánico sin alojar memoria por frame."""
    rng = np.random.default_rng(2026)
    lum = np.clip(128.0 + rng.normal(0.0, 34.0, (side, side)), 0.0, 255.0).astype(np.uint8)
    rgba = np.empty((side, side, 4), dtype=np.uint8)
    rgba[..., 0] = lum
    rgba[..., 1] = lum
    rgba[..., 2] = lum
    rgba[..., 3] = 255
    return skia.Image.fromarray(rgba, colorType=skia.kRGBA_8888_ColorType, copy=True)


_GRAIN_IMAGE: skia.Image | None = None


def _grain_image() -> skia.Image:
    global _GRAIN_IMAGE
    if _GRAIN_IMAGE is None:
        _GRAIN_IMAGE = _make_grain_tile()
    return _GRAIN_IMAGE


class CinematicBackground:
    """Compone los intervalos de video sobre/en lugar del `Background` de la portada."""

    def __init__(
        self,
        config: CinematicConfig | None,
        w: int,
        h: int,
        async_video: bool = False,
        on_frame_ready: Callable[[], None] | None = None,
    ):
        self.w, self.h = w, h
        self.async_video = async_video
        self.on_frame_ready = on_frame_ready
        self._decoders: dict[tuple[str, str, int, int], VideoDecoder] = {}
        self.config: CinematicConfig | None = None
        self.set_config(config)

    def set_config(self, config: CinematicConfig | None):
        self.config = config
        if config is None:
            self.close()
            return
        valid_keys = {
            (r.id, r.path, r.fps_num, r.fps_den)
            for r in config.resources.values()
            if r.path
        }
        for key in list(self._decoders):
            if key not in valid_keys:
                self._decoders.pop(key).close()
        if self.async_video and config.enabled:
            for iv in config.intervals[:4]:
                res = config.video_for(iv)
                if res and res.path:
                    self._decoder_for(res).prefetch_ms(iv.video_start_ms, urgent=False)

    def _decoder_for(self, res: VideoResource) -> VideoDecoder:
        key = (res.id, res.path, res.fps_num, res.fps_den)
        dec = self._decoders.get(key)
        if dec is None:
            dec = VideoDecoder(res, (self.w, self.h), async_decode=self.async_video, on_frame_ready=self.on_frame_ready)
            self._decoders[key] = dec
        return dec

    def _prefetch_upcoming(self, t_ms: float):
        cfg = self.config
        if not (self.async_video and cfg and cfg.enabled):
            return
        for iv in cfg.intervals:
            if t_ms < iv.start_ms <= t_ms + LOOKAHEAD_MS:
                res = cfg.video_for(iv)
                if res and res.path:
                    self._decoder_for(res).prefetch_ms(iv.video_start_ms, urgent=False)

    def active_at(self, t_ms: float) -> bool:
        self._prefetch_upcoming(t_ms)
        return bool(evaluate_at(self.config, t_ms))

    def wait_idle(self, timeout: float = 5.0):
        for dec in list(self._decoders.values()):
            dec.wait_idle(timeout)

    def draw(self, canvas: skia.Canvas, base_bg, t_s: float, lyric_act: float = 0.0):
        t_ms = t_s * 1000.0
        self._prefetch_upcoming(t_ms)
        active = evaluate_at(self.config, t_ms)
        if not active:
            base_bg.draw(canvas, t_s)
            return

        # Resolvemos primero qué imágenes están listas; si ninguna pudo decodificarse, el fondo de portada cubre el lienzo
        layers: list[tuple[ActiveInterval, skia.Image, float]] = []
        max_opaque = 0.0
        for act in active:
            img = self._decoder_for(act.resource).get_image(act.src_ms)
            if img is not None:
                tr = act.interval.treatment.clamped()
                eff_alpha = clamp01(act.envelope * tr.opacity)
                if eff_alpha > 1e-3:
                    layers.append((act, img, eff_alpha))
                    max_opaque = max(max_opaque, eff_alpha)

        if not layers:
            base_bg.draw(canvas, t_s)
            return

        if max_opaque < 0.999:
            base_bg.draw(canvas, t_s)

        rect = skia.Rect.MakeWH(self.w, self.h)
        sampling = skia.SamplingOptions(skia.FilterMode.kLinear)
        for act, img, eff_alpha in layers:
            self._draw_memory_layer(canvas, rect, sampling, img, act.interval.treatment.clamped(),
                                    act.envelope, eff_alpha, t_s, clamp01(lyric_act))

    def _draw_memory_layer(
        self,
        canvas: skia.Canvas,
        rect: skia.Rect,
        sampling: skia.SamplingOptions,
        img: skia.Image,
        tr: CinematicTreatment,
        envelope: float,
        eff_alpha: float,
        t_s: float,
        lyric_act: float,
    ):
        # 1. Desenfoque suave + dinámico: se condensa al entrar/salir y se suaviza levemente cuando hay verso activo
        ref = min(self.w, self.h)
        base_sigma = tr.blur * 0.032 * ref
        transition_sigma = (1.0 - envelope) * 0.024 * ref * max(0.25, tr.blur)
        lyric_sigma = (0.006 * ref + base_sigma * 0.22) * lyric_act
        sigma = base_sigma + transition_sigma + lyric_sigma
        img_filter = skia.ImageFilters.Blur(sigma, sigma, skia.TileMode.kClamp) if sigma > 0.35 else None

        color_filter = (
            _color_matrix(tr.saturation, tr.contrast)
            if abs(tr.saturation - 1.0) > 1e-3 or abs(tr.contrast - 1.0) > 1e-3
            else None
        )
        paint = skia.Paint(
            AntiAlias=True,
            Alphaf=eff_alpha,
            ColorFilter=color_filter,
            ImageFilter=img_filter,
        )
        canvas.drawImageRect(img, rect, sampling, paint)

        # 2. Velo de legibilidad (ligeramente más profundo mientras se canta una línea activa)
        veil = clamp01(tr.dim * (1.0 + 0.14 * lyric_act)) * eff_alpha
        if veil > 0.003:
            canvas.drawColor(skia.ColorSetARGB(round(veil * 255), 0, 0, 0))

        # 3. Viñeta radial suave
        vig = tr.vignette * 0.68 * eff_alpha
        if vig > 0.004:
            rad = 0.62 * math.hypot(self.w, self.h)
            shader = skia.GradientShader.MakeRadial(
                (self.w / 2.0, self.h / 2.0),
                rad,
                [skia.ColorSetARGB(0, 0, 0, 0), skia.ColorSetARGB(round(vig * 255), 0, 0, 0)],
                [0.28, 1.0],
            )
            canvas.drawRect(rect, skia.Paint(Shader=shader))

        # 4. Grano de película sutil a cadencia cinematográfica determinista (24 Hz en función de `t_s`, nunca reloj real)
        grain_a = tr.grain * eff_alpha
        if grain_a > 0.004:
            tick = int(math.floor(max(0.0, t_s) * 24.0))
            ox = float((tick * 73) % GRAIN_TILE)
            oy = float((tick * 137) % GRAIN_TILE)
            g_shader = _grain_image().makeShader(
                skia.TileMode.kRepeat,
                skia.TileMode.kRepeat,
                sampling,
                skia.Matrix.Translate(ox, oy),
            )
            canvas.drawRect(rect, skia.Paint(Shader=g_shader, Alphaf=grain_a, BlendMode=skia.BlendMode.kSoftLight))

    def close(self):
        for dec in list(self._decoders.values()):
            dec.close()
        self._decoders.clear()
