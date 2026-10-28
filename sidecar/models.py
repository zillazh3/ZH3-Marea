"""Modelos de alineación CTC por idioma. Descargan de Hugging Face bajo demanda a HF_HOME (el cliente lo apunta a lab/hf)."""
import json

from sidecar.stages import StageError

# wav2vec2 ajustado con CTC. Licencias y tamaños verificados en la API de HF el 2026-10-27: ambos Apache-2.0 (360 MB / 1204 MB).
MODELS = {"en": "facebook/wav2vec2-base-960h", "es": "jonatasgrosman/wav2vec2-large-xlsr-53-spanish"}


def repo_for(lang: str) -> str:
    if lang not in MODELS:
        raise StageError(f"Todavía no hay modelo de alineación para «{lang}». Idiomas disponibles: {', '.join(sorted(MODELS))}.")
    return MODELS[lang]


MISSING = "El modelo «{}» no está descargado. Descárgalo primero (es una descarga explícita: la alineación nunca usa la red)."


def fetch(repo: str) -> str:
    """Descarga el modelo a HF_HOME (única función que usa la red). Devuelve la carpeta local."""
    from huggingface_hub import constants, snapshot_download
    if constants.HF_HUB_OFFLINE:
        raise StageError("Este proceso trabaja sin red. La descarga de modelos va en un proceso aparte iniciado en modo descarga.")
    try:
        return snapshot_download(repo, allow_patterns=["*.json", "pytorch_model.bin", "model.safetensors"])
    except Exception as e:
        raise StageError(f"No pude descargar «{repo}» ({type(e).__name__}). Revisa tu conexión.") from e


def load_vocab(repo: str) -> tuple[dict[str, int], int]:
    """(letra en minúscula → id, id del blank). `|` (separador de palabras) y los tokens especiales no son letras."""
    from huggingface_hub import hf_hub_download
    try:
        path = hf_hub_download(repo, "vocab.json", local_files_only=True)
    except Exception as e:
        raise StageError(MISSING.format(repo)) from e
    with open(path, encoding="utf-8") as f:
        v = json.load(f)
    return {k.lower(): i for k, i in v.items() if len(k) == 1 and k != "|"}, v["<pad>"]
