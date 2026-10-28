"""Etapas del pipeline y su caché. Cada etapa escribe un artefacto en `cache/<sha256 del archivo de audio>/<etapa>-<hash de parámetros>`;
si ya existe se reutiliza sin recalcular. La escritura es atómica: una cancelación o un cierre a medias no deja un artefacto roto."""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RATE = 16000  # lo que esperan wav2vec2 y Whisper


class Cancelled(Exception):
    pass


class StageError(Exception):
    """Error con mensaje listo para mostrar al usuario."""


class Ctx:
    """Lo que una etapa necesita saber de la petición en curso."""

    def __init__(self, req_id: str, send, cache_dir: Path, cancelled_ids: set):
        self.id, self.send, self.cache_dir, self._cancelled = req_id, send, Path(cache_dir), cancelled_ids

    def check(self):
        if self.id in self._cancelled:
            raise Cancelled()


def params_key(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def run_stage(ctx: Ctx, audio_sha: str, name: str, params: dict, ext: str, compute) -> Path:
    """Devuelve la ruta del artefacto. `compute(tmp_path)` lo escribe en `tmp_path`; aquí se publica con un rename atómico.
    Emite `stage` con estado cached | start → done | error (solo lo que realmente ocurrió)."""
    path = ctx.cache_dir / audio_sha / f"{name}-{params_key(params)}.{ext}"
    if path.exists():
        ctx.send(event="stage", id=ctx.id, name=name, state="cached", seconds=0.0)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    ctx.send(event="stage", id=ctx.id, name=name, state="start")
    t0 = time.perf_counter()
    try:
        info = compute(tmp)  # si devuelve un dict (dispositivo, VRAM pico…) viaja en el evento `done`
        os.replace(tmp, path)
    except BaseException as e:
        tmp.unlink(missing_ok=True)
        if not isinstance(e, Cancelled):
            ctx.send(event="stage", id=ctx.id, name=name, state="error", seconds=round(time.perf_counter() - t0, 3))
        raise
    ctx.send(event="stage", id=ctx.id, name=name, state="done", seconds=round(time.perf_counter() - t0, 3),
             detail=info if isinstance(info, dict) else {})
    return path


def file_sha256(path: str, ctx: Ctx) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                ctx.check()
                h.update(chunk)
    except OSError as e:
        raise StageError(f"No pude abrir el archivo de audio: {e.strerror or e}") from e
    return h.hexdigest()


def ffmpeg_pcm(ctx: Ctx, path: str, ffmpeg: str) -> np.ndarray:
    """Cualquier archivo de audio → mono 16 kHz float32, cancelable."""
    try:
        proc = subprocess.Popen([ffmpeg, "-v", "error", "-i", str(path), "-vn", "-f", "f32le", "-ac", "1", "-ar", str(RATE), "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError as e:
        raise StageError("No pude ejecutar ffmpeg.") from e
    chunks = []
    try:
        while chunk := proc.stdout.read(1 << 20):
            ctx.check()
            chunks.append(chunk)
    except Cancelled:
        proc.kill()
        raise
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code != 0 or not chunks:
        raise StageError("No pude leer ese archivo de audio. Usa un MP3, WAV, FLAC, M4A u OGG válido.")
    usable = b"".join(chunks)
    return np.frombuffer(usable[:len(usable) // 4 * 4], dtype=np.float32)


def decode(ctx: Ctx, audio: str, ffmpeg: str) -> dict:
    """Audio → PCM mono 16 kHz float32 (.npy). La caché se indexa por el contenido del archivo: buscarla no cuesta decodificar."""
    sha = file_sha256(audio, ctx)

    def compute(tmp: Path):
        with open(tmp, "wb") as f:
            np.save(f, ffmpeg_pcm(ctx, audio, ffmpeg))

    path = run_stage(ctx, sha, "decode", {"rate": RATE, "channels": 1}, "npy", compute)
    n = int(np.load(path, mmap_mode="r").shape[0])
    return {"audio_sha256": sha, "pcm_path": str(path), "samples": n, "rate": RATE, "duration_ms": round(n * 1000 / RATE)}


# ---------------- emisiones CTC (modelo) ----------------

CHUNK_FRAMES, CONTEXT_FRAMES = 3000, 100  # 60 s por pasada + 2 s de contexto a cada lado que se descarta
HOP, WIN = 320, 400  # muestras: stride y campo receptivo de wav2vec2


def emissions(ctx: Ctx, sha: str, pcm_path: str, repo: str, dev: dict, source: str = "mix") -> Path:
    """log P(letra | frame) de toda la canción, (frames × vocabulario) float32. Por fragmentos: la memoria no crece con la duración.
    El audio se normaliza con la media y desviación de la canción completa (como el extractor del modelo, pero sin que cada
    fragmento se normalice distinto)."""
    use_cuda = dev["device"] == "cuda"
    params = {"model": repo, "source": source, "dtype": "float16" if use_cuda else "float32", "chunk": CHUNK_FRAMES, "context": CONTEXT_FRAMES}

    def compute(tmp: Path):
        import torch
        from transformers import Wav2Vec2ForCTC

        pcm = np.load(pcm_path)
        if len(pcm) < WIN:
            raise StageError("El audio es demasiado corto.")
        pcm = ((pcm - pcm.mean()) / np.sqrt(pcm.var() + 1e-7)).astype(np.float32)
        total = (len(pcm) - WIN) // HOP + 1
        torch_dev = torch.device("cuda" if use_cuda else "cpu")
        dtype = torch.float16 if use_cuda else torch.float32
        if use_cuda:
            torch.cuda.reset_peak_memory_stats()
        try:
            # local_files_only: la inferencia nunca usa la red (la descarga es el comando explícito fetch_model)
            model = Wav2Vec2ForCTC.from_pretrained(repo, torch_dtype=dtype, local_files_only=True).to(torch_dev).eval()
        except OSError as e:
            from sidecar.models import MISSING
            raise StageError(MISSING.format(repo)) from e
        out = None
        try:
            for s in range(0, total, CHUNK_FRAMES):
                ctx.check()
                e, a = min(s + CHUNK_FRAMES, total), max(0, s - CONTEXT_FRAMES)
                b = min(total, e + CONTEXT_FRAMES)
                x = torch.from_numpy(pcm[a * HOP:(b - 1) * HOP + WIN])[None].to(torch_dev, dtype)
                with torch.inference_mode():
                    logp = torch.log_softmax(model(x).logits.float(), -1)[0].cpu().numpy()
                if logp.shape[0] != b - a:
                    raise StageError("El modelo devolvió un número de frames inesperado.")
                if not np.isfinite(logp).all():
                    raise StageError("La GPU produjo valores no válidos (fp16) con este audio.")
                if out is None:
                    out = np.empty((total, logp.shape[1]), np.float32)
                out[s:e] = logp[s - a:s - a + (e - s)]
            peak = round(torch.cuda.max_memory_allocated() / 2**20) if use_cuda else 0
        finally:
            del model
            if use_cuda:
                torch.cuda.empty_cache()
        with open(tmp, "wb") as f:
            np.save(f, out)
        return {"device": dev["device"], "dtype": params["dtype"], "frames": total, "peak_vram_mb": peak}

    return run_stage(ctx, sha, "emissions", params, "npy", compute)


def energy(ctx: Ctx, sha: str, pcm_path: str, source: str = "mix") -> Path:
    from sidecar.pipeline import frame_energy

    def compute(tmp: Path):
        with open(tmp, "wb") as f:
            np.save(f, frame_energy(np.load(pcm_path)))

    return run_stage(ctx, sha, "energy", {"band": [200, 4000], "smooth": 3, "source": source}, "npy", compute)


def align(ctx: Ctx, audio: str, ffmpeg: str, lang: str, lines: list, use_energy: bool = True, syllables: bool = True,
          device: str = "auto", model: str = "", separate: str = "", window: list | None = None) -> dict:
    """Alinea la letra dada con el audio. Cada línea es `{"id", "words": [[sílabas]]}` (Modo A, con la segmentación de la fuente) o
    `{"id", "text"}` (Modo B: el sidecar parte el texto en palabras y, en español, en sílabas). decode → emissions → energy → align, cada
    etapa cacheada por separado: cambiar solo el texto reutiliza las emisiones sin cargar ningún modelo.
    `window=[inicio_ms, fin_ms]` alinea solo ese tramo de las emisiones (recalcular una sección sin reprocesar la canción)."""
    from sidecar import device as dev_mod
    from sidecar.models import load_vocab, repo_for
    from sidecar.pipeline import FRAME_MS, align_lines, shift_times
    from sidecar.syllables import split_text

    lines = [{**l, "words": l["words"]} if l.get("words") else {**l, "words": split_text(l.get("text", ""), lang)} for l in lines]
    if not lines or not all(l["words"] for l in lines):
        raise StageError("No hay letra que alinear.")
    if window is not None and not (len(window) == 2 and 0 <= window[0] < window[1]):
        raise StageError("El tramo a recalcular no es válido.")
    d = decode(ctx, audio, ffmpeg)
    sha = d["audio_sha256"]
    repo = model or repo_for(lang)  # `model`: otro repo de HF con el mismo formato de vocabulario (para comparar modelos)
    # la clave de la caché del alineado incluye el dispositivo elegido (fp16 vs fp32 dan emisiones ligeramente distintas)
    dev = dev_mod.pick_device() if device == "auto" else {"device": device, "reason": "forzado", "name": "", "capability": None, "total_gb": 0.0}
    pcm, source = d["pcm_path"], "mix"
    if separate:  # voz aislada: el modelo CTC y la energía trabajan sobre el stem, no sobre la mezcla
        pcm, source = str(separate_vocals(ctx, sha, audio, ffmpeg, separate, dev)), f"vocals:{separate}"
    em = emissions(ctx, sha, pcm, repo, dev, source)
    en = energy(ctx, sha, pcm, source) if use_energy else None

    def compute(tmp: Path):
        vocab, blank = load_vocab(repo)
        logp = np.load(em)
        e = np.load(en) if en else None
        if e is not None:
            e = e[:len(logp)]
        f0 = 0
        if window is not None:  # solo ese tramo de las emisiones; los tiempos se devuelven en el reloj de la canción
            f0, f1 = max(0, int(window[0] // FRAME_MS)), min(len(logp), int(-(-window[1] // FRAME_MS)))
            logp, e = logp[f0:f1], (e[f0:f1] if e is not None else None)
        res = align_lines(logp, [l["words"] for l in lines], vocab, blank=blank, energy=e, lang=lang, syllables=syllables)
        if f0:
            shift_times(res, round(f0 * FRAME_MS))
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([{**r, "id": l["id"]} for r, l in zip(res, lines)], f, ensure_ascii=False)

    key = {"lines": lines, "lang": lang, "model": repo, "energy": use_energy, "syllables": syllables, "device": dev["device"], "source": source,
           "window": window}
    path = run_stage(ctx, sha, "align", key, "json", compute)
    return {"audio_sha256": sha, "duration_ms": d["duration_ms"], "language": lang, "model": repo, "device": dev, "source": source, "lines": json.loads(path.read_text("utf-8"))}


# ---------------- separación de voz ----------------

SEPARATOR_DEFAULT = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"  # 639 MB; el repo de modelos (TRvlvr/model_repo) NO declara licencia


def models_dir(ctx: Ctx) -> Path:
    return ctx.cache_dir.parent / "separator-models"


def fetch_separator(ctx: Ctx, model: str = SEPARATOR_DEFAULT) -> dict:
    """Descarga explícita del modelo de separación (necesita red: solo en un proceso iniciado en modo descarga)."""
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        raise StageError("Este proceso trabaja sin red. La descarga de modelos va en un proceso aparte iniciado en modo descarga.")
    from audio_separator.separator import Separator
    d = models_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    ctx.send(event="stage", id=ctx.id, name="download", state="start", detail={"model": model})
    t0 = time.perf_counter()
    try:
        Separator(model_file_dir=str(d), log_level=40).download_model_and_data(model)
    except Exception as e:
        raise StageError(f"No pude descargar el modelo de separación ({type(e).__name__}). Revisa tu conexión.") from e
    ctx.send(event="stage", id=ctx.id, name="download", state="done", seconds=round(time.perf_counter() - t0, 3), detail={"model": model})
    return {"model": model, "files": sorted(p.name for p in d.iterdir())}


def separate_vocals(ctx: Ctx, sha: str, audio: str, ffmpeg: str, model: str, dev: dict) -> Path:
    """Voz aislada de la canción (mono 16 kHz .npy). La carga es local: si el modelo no está descargado, error claro (nunca descarga solo)."""
    d = models_dir(ctx)
    if not (d / model).exists():
        raise StageError(f"El modelo de separación «{model}» no está descargado. Descárgalo primero (es una descarga explícita).")
    use_cuda = dev["device"] == "cuda"

    def compute(tmp: Path):
        import shutil
        import tempfile

        import torch
        from audio_separator.separator import Separator

        work = Path(tempfile.mkdtemp(prefix="sep-", dir=ctx.cache_dir.parent))
        try:
            if use_cuda:
                torch.cuda.reset_peak_memory_stats()
            # use_autocast: 2.4× más rápido que fp32 con un stem prácticamente idéntico (70 dB frente al de fp32, medido en la RTX 5060)
            sep = Separator(model_file_dir=str(d), output_dir=str(work), output_format="WAV", output_single_stem="Vocals", log_level=40,
                            use_autocast=use_cuda)
            sep.load_model(model)
            ctx.check()
            names = sep.separate(audio)
            ctx.check()
            stem = next((work / n for n in names if "ocals" in n), None) or (work / names[0])
            pcm = ffmpeg_pcm(ctx, str(stem), ffmpeg)
            peak = round(torch.cuda.max_memory_allocated() / 2**20) if use_cuda else 0
            del sep
            if use_cuda:
                torch.cuda.empty_cache()
            with open(tmp, "wb") as f:
                np.save(f, pcm)
            return {"device": dev["device"], "peak_vram_mb": peak, "seconds_of_audio": round(len(pcm) / RATE, 1)}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return run_stage(ctx, sha, "separate", {"model": model}, "npy", compute)


# ---------------- transcripción (Modo B: no hay letra) ----------------

ASR_DEFAULT = "large-v3-turbo"  # faster-whisper; ~1.6 GB. MIT (código y pesos de Whisper)
NO_SPEECH_ABOVE, LOGPROB_BELOW = 0.6, -1.0  # los umbrales de Whisper para «probablemente no hay voz» / «texto poco fiable»


def fetch_asr(ctx: Ctx, model: str = ASR_DEFAULT) -> dict:
    """Descarga explícita del modelo de transcripción (necesita red: solo en un proceso iniciado en modo descarga)."""
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        raise StageError("Este proceso trabaja sin red. La descarga de modelos va en un proceso aparte iniciado en modo descarga.")
    from faster_whisper.utils import download_model
    ctx.send(event="stage", id=ctx.id, name="download", state="start", detail={"model": model})
    t0 = time.perf_counter()
    try:
        path = download_model(model)
    except Exception as e:
        raise StageError(f"No pude descargar el modelo de transcripción «{model}» ({type(e).__name__}). Revisa tu conexión.") from e
    ctx.send(event="stage", id=ctx.id, name="download", state="done", seconds=round(time.perf_counter() - t0, 3), detail={"model": model})
    return {"model": model, "path": str(path)}


def asr(ctx: Ctx, audio: str, ffmpeg: str, model: str = ASR_DEFAULT, lang: str = "", separate: str = "", device: str = "auto") -> dict:
    """Borrador de letra: texto por línea (con tiempos aproximados de Whisper, que NO son el resultado final) e idioma detectado. Es solo un
    borrador editable: la sincronía real sale de alinear el texto ya corregido (`align`). Los segmentos con poca voz o poca confianza vienen
    marcados `suspect` (Whisper alucina texto sobre instrumentales)."""
    from sidecar import device as dev_mod

    d = decode(ctx, audio, ffmpeg)
    sha = d["audio_sha256"]
    dev = dev_mod.pick_device() if device == "auto" else {"device": device, "reason": "forzado", "name": "", "capability": None, "total_gb": 0.0}
    pcm, source = d["pcm_path"], "mix"
    if separate:
        pcm, source = str(separate_vocals(ctx, sha, audio, ffmpeg, separate, dev)), f"vocals:{separate}"
    use_cuda = dev["device"] == "cuda"

    def compute(tmp: Path):
        import gc

        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        try:
            download_model(model, local_files_only=True)
        except Exception as e:
            raise StageError(f"El modelo de transcripción «{model}» no está descargado. Descárgalo primero (es una descarga explícita).") from e
        m = WhisperModel(model, device="cuda" if use_cuda else "cpu", compute_type="float16" if use_cuda else "int8", local_files_only=True)
        try:
            segments, info = m.transcribe(np.load(pcm), language=lang or None, beam_size=5, condition_on_previous_text=False)
            lines = []
            for s in segments:  # generador perezoso: se puede cancelar entre segmentos
                ctx.check()
                text = s.text.strip()
                if text:
                    lines.append({"text": text, "start": round(s.start * 1000), "end": round(s.end * 1000), "avg_logprob": round(s.avg_logprob, 3),
                                  "no_speech_prob": round(s.no_speech_prob, 3), "suspect": s.no_speech_prob > NO_SPEECH_ABOVE or s.avg_logprob < LOGPROB_BELOW})
        finally:
            del m
            gc.collect()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"language": info.language, "language_probability": round(info.language_probability, 3), "lines": lines}, f, ensure_ascii=False)
        return {"device": dev["device"], "language": info.language}

    key = {"model": model, "lang": lang, "source": source, "device": dev["device"]}
    path = run_stage(ctx, sha, "asr", key, "json", compute)
    return {"audio_sha256": sha, "duration_ms": d["duration_ms"], "model": model, "device": dev, "source": source, **json.loads(path.read_text("utf-8"))}
