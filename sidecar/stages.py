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


def _is_secondary_line(l: dict) -> bool:
    if l.get("source_stem"):
        return l["source_stem"] == "backing"
    role = l.get("role") or ("backing" if l.get("bg") else "")
    return bool(l.get("bg")) or role in ("backing", "harmony", "adlib")


def _passthrough_meta(src: dict) -> dict:
    return {k: src[k] for k in ("agent", "bg", "role", "role_confidence", "layer", "parent", "standalone", "review_state") if k in src}


def align(ctx: Ctx, audio: str, ffmpeg: str, lang: str, lines: list, use_energy: bool = True, syllables: bool = True,
          device: str = "auto", model: str = "", separate: str = "", karaoke: str = "", window: list | None = None) -> dict:
    """Alinea la letra dada con el audio. Cada línea es `{"id", "words": [[sílabas]]}` (Modo A, con la segmentación de la fuente) o
    `{"id", "text"}` (Modo B: el sidecar parte el texto en palabras y, en español, en sílabas). decode → emissions → energy → align, cada
    etapa cacheada por separado: cambiar solo el texto reutiliza las emisiones sin cargar ningún modelo.
    Las capas simultáneas o voces de fondo se alinean en pasadas independientes (sobre su propio stem si `karaoke` está activo, o en
    independent windows on a shared stem) without clipping or shifting another performance.
    `window=[inicio_ms, fin_ms]` alinea solo ese tramo de las emisiones (recalcular una sección sin reprocesar la canción)."""
    from sidecar import device as dev_mod
    from sidecar.layers import layer_window, partition_streams
    from sidecar.models import load_vocab, repo_for
    from sidecar.pipeline import FRAME_MS, align_lines, shift_times
    from sidecar.syllables import split_text

    lines = [{**l, "words": l["words"]} if l.get("words") else {**l, "words": split_text(l.get("text", ""), lang)} for l in lines]
    if not karaoke and any(l.get("source_stem") in ("lead", "backing") for l in lines):
        raise StageError("Enable local lead/backing separation to realign performances from separated stems.")
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
    back_em = back_en = None
    if karaoke:
        v_mod = separate or SEPARATOR_DEFAULT
        lead_p, back_p = separate_lead_backing(ctx, sha, audio, ffmpeg, v_mod, karaoke, dev)
        pcm, source = str(lead_p), f"lead:{v_mod}:{karaoke}"
        if any(_is_secondary_line(l) for l in lines):
            back_src = f"backing:{v_mod}:{karaoke}"
            back_em = emissions(ctx, sha, str(back_p), repo, dev, back_src)
            back_en = energy(ctx, sha, str(back_p), back_src) if use_energy else None
    elif separate:  # voz aislada completa (todas las voces juntas, sin instrumentos)
        pcm, source = str(separate_vocals(ctx, sha, audio, ffmpeg, separate, dev)), f"vocals:{separate}"
    em = emissions(ctx, sha, pcm, repo, dev, source)
    en = energy(ctx, sha, pcm, source) if use_energy else None

    def _run_window(lp: np.ndarray, eg: np.ndarray | None, group_words: list, win: tuple[int, int] | None,
                    vocab: dict, blank: int) -> list[dict]:
        f0, f1 = (0, len(lp)) if win is None else (max(0, int(win[0] // FRAME_MS)), min(len(lp), int(-(-win[1] // FRAME_MS))))
        if f1 <= f0:
            raise ValueError("The alignment window contains no audio.")
        res = align_lines(lp[f0:f1], group_words, vocab, blank=blank,
                          energy=eg[f0:f1] if eg is not None else None, lang=lang, syllables=syllables)
        if f0:
            shift_times(res, round(f0 * FRAME_MS))
        return res

    def compute(tmp: Path):
        vocab, blank = load_vocab(repo)
        logp = np.load(em)
        e = np.load(en)[:len(logp)] if en else None
        b_logp = np.load(back_em) if back_em else None
        b_e = np.load(back_en)[:len(b_logp)] if (back_en and b_logp is not None) else None

        backbone, secondary = partition_streams(lines)
        groups = ([backbone] if backbone else []) + secondary
        by_idx = {}
        for group in groups:
            ctx.check()
            use_back = b_logp is not None and all(_is_secondary_line(lines[i]) for i in group)
            lp_src, eg_src = (b_logp, b_e) if use_back else (logp, e)
            own_window = layer_window(lines[group[0]], {}, 0, d["duration_ms"]) if len(group) == 1 else None
            win = tuple(window) if window is not None else own_window
            try:
                results = _run_window(lp_src, eg_src, [lines[i]["words"] for i in group], win, vocab, blank)
            except ValueError:
                if not all(lines[i].get("fallback") for i in group):
                    raise StageError("The section cannot be aligned reliably; correct its text or timing window.")
                results = [json.loads(json.dumps(lines[i]["fallback"])) for i in group]
                for result in results:
                    result["approximate"], result["confidence"] = True, 0.0
                    for word in result["words"]:
                        for unit in [word, *word.get("syllables", [])]:
                            unit["approximate"], unit["confidence"] = True, 0.0
            for i, result in zip(group, results):
                result["source_stem"] = ("backing" if use_back else "lead") if karaoke else ("vocals" if separate else "mix")
                if not karaoke and (len(groups) > 1 or _is_secondary_line(lines[i])):
                    # Shared-stem energy cannot establish which performance sustained a note.
                    result["approximate"] = True
                    for word in result["words"]:
                        word["approximate"] = True
                        for unit in word.get("syllables", []):
                            unit["approximate"] = True
                by_idx[i] = result
        out = [{**by_idx[i], "id": line["id"], **_passthrough_meta(line)} for i, line in enumerate(lines)]

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

    key = {"lines": lines, "lang": lang, "model": repo, "energy": use_energy, "syllables": syllables, "device": dev["device"], "source": source,
           "karaoke": karaoke, "window": window, "vocal_layers_version": 2}
    path = run_stage(ctx, sha, "align", key, "json", compute)
    return {"audio_sha256": sha, "duration_ms": d["duration_ms"], "language": lang, "model": repo, "device": dev, "source": source, "separate": separate, "karaoke": karaoke, "lines": json.loads(path.read_text("utf-8"))}


# ---------------- separación de voz ----------------

SEPARATOR_DEFAULT = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"  # 639 MB; el repo de modelos (TRvlvr/model_repo) NO declara licencia
KARAOKE_DEFAULT = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"  # Weights are supplied separately; redistribution license unresolved.


def karaoke_stem_names(primary: str, secondary: str) -> dict[str, str]:
    """Map explicit model labels, never the order of returned files."""
    mapping = {"karaoke": "backing", "other": "lead", "lead_only": "lead", "backing_only": "backing",
               "Lead Vocals": "lead", "Backing Vocals": "backing"}
    names = {label: mapping[label] for label in (primary, secondary) if label in mapping}
    if set(names.values()) != {"lead", "backing"}:
        raise StageError("The karaoke model does not declare an unambiguous lead/backing stem pair.")
    return names


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


def separate_lead_backing(ctx: Ctx, sha: str, audio: str, ffmpeg: str, vocal_model: str, karaoke_model: str,
                          dev: dict) -> tuple[Path, Path]:
    """Separa en dos etapas: (1) todas las voces en estéreo 44.1 kHz sin instrumentos y (2) voz principal (`lead`) vs coros/armonías/ad-libs
    (`backing`). Nunca descarga modelos implícitamente; si falta alguno, falla con mensaje claro."""
    d = models_dir(ctx)
    for m in (vocal_model, karaoke_model):
        if not (d / m).exists():
            raise StageError(f"El modelo de separación «{m}» no está descargado. Descárgalo primero (es una descarga explícita).")
    key = params_key({"vocal_model": vocal_model, "karaoke_model": karaoke_model, "vocal_layers_version": 2})
    lead_path = ctx.cache_dir / sha / f"lead-{key}.npy"
    back_path = ctx.cache_dir / sha / f"backing-{key}.npy"
    if lead_path.exists() and back_path.exists():
        ctx.send(event="stage", id=ctx.id, name="separate_layers", state="cached", seconds=0.0)
        return lead_path, back_path
    lead_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_l = lead_path.with_name(lead_path.name + f".{os.getpid()}.tmp")
    tmp_b = back_path.with_name(back_path.name + f".{os.getpid()}.tmp")
    ctx.send(event="stage", id=ctx.id, name="separate_layers", state="start")
    t0 = time.perf_counter()
    use_cuda = dev["device"] == "cuda"
    import shutil
    import tempfile

    import torch
    from audio_separator.separator import Separator

    work = Path(tempfile.mkdtemp(prefix="sep2-", dir=ctx.cache_dir.parent))
    try:
        if use_cuda:
            torch.cuda.reset_peak_memory_stats()
        sep1 = Separator(model_file_dir=str(d), output_dir=str(work), output_format="WAV", output_single_stem="Vocals",
                         log_level=40, use_autocast=use_cuda)
        sep1.load_model(vocal_model)
        ctx.check()
        n1 = sep1.separate(audio, custom_output_names={"Vocals": "all_vocals"})
        del sep1
        if use_cuda:
            torch.cuda.empty_cache()
        ctx.check()
        vocal_wav = work / "all_vocals.wav"
        if not vocal_wav.is_file() or len(n1) != 1:
            raise StageError("The vocal separator did not return its declared Vocals stem.")

        work2 = work / "stage2"
        work2.mkdir(exist_ok=True)
        sep2 = Separator(model_file_dir=str(d), output_dir=str(work2), output_format="WAV", log_level=40, use_autocast=use_cuda)
        sep2.load_model(karaoke_model)
        labels = karaoke_stem_names(sep2.model_instance.primary_stem_name, sep2.model_instance.secondary_stem_name)
        ctx.check()
        n2 = sep2.separate(str(vocal_wav), custom_output_names=labels)
        peak = round(torch.cuda.max_memory_allocated() / 2**20) if use_cuda else 0
        del sep2
        if use_cuda:
            torch.cuda.empty_cache()
        ctx.check()
        lead_wav, back_wav = work2 / "lead.wav", work2 / "backing.wav"
        if len(n2) != 2 or not lead_wav.is_file() or not back_wav.is_file():
            raise StageError("The karaoke separator did not return both declared vocal stems.")
        lead_pcm = ffmpeg_pcm(ctx, str(lead_wav), ffmpeg)
        back_pcm = ffmpeg_pcm(ctx, str(back_wav), ffmpeg)
        if len(lead_pcm) != len(back_pcm):
            raise StageError("Separated vocal stems have different durations; refusing to shift or trim them.")
        with open(tmp_l, "wb") as f:
            np.save(f, lead_pcm)
        with open(tmp_b, "wb") as f:
            np.save(f, back_pcm)
        os.replace(tmp_l, lead_path)
        os.replace(tmp_b, back_path)
    except BaseException as e:
        tmp_l.unlink(missing_ok=True)
        tmp_b.unlink(missing_ok=True)
        if not isinstance(e, Cancelled):
            ctx.send(event="stage", id=ctx.id, name="separate_layers", state="error", seconds=round(time.perf_counter() - t0, 3))
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ctx.send(event="stage", id=ctx.id, name="separate_layers", state="done", seconds=round(time.perf_counter() - t0, 3),
             detail={"device": dev["device"], "peak_vram_mb": peak})
    return lead_path, back_path


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


def asr(ctx: Ctx, audio: str, ffmpeg: str, model: str = ASR_DEFAULT, lang: str = "", separate: str = "",
        karaoke: str = "", device: str = "auto") -> dict:
    """Borrador de letra: texto por línea (con tiempos aproximados de Whisper, que NO son el resultado final) e idioma detectado.
    Con `karaoke` activo transcribe por separado la voz principal y el stem secundario (rechazando fugas acústicas del separador y
    clasificando coros/armonías/ad-libs/duetos/inciertos sin concatenarlos jamás en el verso principal). Sin `karaoke`, detecta
    solapes polifónicos en el stem común y los reporta como `uncertain` sin fingir haber separado las voces."""
    from sidecar import device as dev_mod
    from sidecar.layers import classify_layer, detect_polyphonic_overlaps, has_active_backing, stem_metrics

    d = decode(ctx, audio, ffmpeg)
    sha = d["audio_sha256"]
    dev = dev_mod.pick_device() if device == "auto" else {"device": device, "reason": "forzado", "name": "", "capability": None, "total_gb": 0.0}
    pcm, back_pcm_path, source = d["pcm_path"], None, "mix"
    if karaoke:
        v_mod = separate or SEPARATOR_DEFAULT
        lead_p, back_p = separate_lead_backing(ctx, sha, audio, ffmpeg, v_mod, karaoke, dev)
        pcm, back_pcm_path, source = str(lead_p), str(back_p), f"layers:{v_mod}:{karaoke}"
    elif separate:
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
        lead_arr = np.load(pcm)
        back_arr = np.load(back_pcm_path) if back_pcm_path else None
        m = WhisperModel(model, device="cuda" if use_cuda else "cpu", compute_type="float16" if use_cuda else "int8", local_files_only=True)
        try:
            segments, info = m.transcribe(lead_arr, language=lang or None, beam_size=5, condition_on_previous_text=False)
            lines = []
            for s in segments:  # generador perezoso: se puede cancelar entre segmentos
                ctx.check()
                text = s.text.strip()
                if text:
                    suspect = s.no_speech_prob > NO_SPEECH_ABOVE or s.avg_logprob < LOGPROB_BELOW
                    entry = {"text": text, "start": round(s.start * 1000), "end": round(s.end * 1000),
                             "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                             "suspect": suspect}
                    entry.update(role="lead" if back_arr is not None else "uncertain", role_confidence=0.0,
                                 agent="", bg=False, source_stem="lead" if back_arr is not None else ("vocals" if separate else "mix"))
                    lines.append(entry)

            overlaps = []
            if back_arr is not None and has_active_backing(lead_arr, back_arr):
                lead_iv = [(l["start"], l["end"]) for l in lines if not l.get("suspect")]
                det_lang = lang or info.language or None
                b_segs, _ = m.transcribe(back_arr, language=det_lang, beam_size=5, condition_on_previous_text=False)
                for s in b_segs:
                    ctx.check()
                    text = s.text.strip()
                    if not text:
                        continue
                    s_ms, e_ms = round(s.start * 1000), round(s.end * 1000)
                    suspect = s.no_speech_prob > NO_SPEECH_ABOVE or s.avg_logprob < LOGPROB_BELOW
                    met = stem_metrics(lead_arr, back_arr, s_ms, e_ms)
                    role, rconf = classify_layer(start_ms=s_ms, end_ms=e_ms, lead_intervals=lead_iv,
                                                 lead_pcm=lead_arr, back_pcm=back_arr, on_backing_stem=True)
                    is_bg = role in ("backing", "harmony", "adlib") or role == "uncertain"
                    if met["is_bleed"] or suspect:
                        role, rconf, is_bg = "uncertain", 0.0, True
                    lines.append({
                        "text": text, "start": s_ms, "end": e_ms,
                        "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                        "suspect": suspect or role == "uncertain",
                        "role": role, "role_confidence": min(rconf, 0.55),
                        "agent": "", "source_stem": "backing", "review_state": "pending",
                        "bg": is_bg,
                    })
                lines.sort(key=lambda l: (l["start"], int(bool(l.get("bg")))))
            elif back_arr is None:
                overlaps = detect_polyphonic_overlaps(lead_arr, lines)
        finally:
            del m
            gc.collect()
        payload = {"language": info.language, "language_probability": round(info.language_probability, 3), "lines": lines}
        if overlaps:
            payload["overlaps"] = overlaps
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return {"device": dev["device"], "language": info.language}

    key = {"model": model, "lang": lang, "source": source, "karaoke": karaoke, "device": dev["device"], "vocal_layers_version": 2}
    path = run_stage(ctx, sha, "asr", key, "json", compute)
    return {"audio_sha256": sha, "duration_ms": d["duration_ms"], "model": model, "device": dev, "source": source,
            "separate": separate, "karaoke": karaoke, **json.loads(path.read_text("utf-8"))}
