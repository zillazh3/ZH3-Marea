"""Audio local: decodifica cualquier archivo con ffmpeg, calcula la forma de onda y recorta con un offset ajustable."""
import subprocess
from pathlib import Path

import numpy as np

from marea.audio.align import apply_fades
from marea.audio.loopback import AudioError
from marea.export.ffmpeg import NO_WINDOW, find_ffmpeg

RATE = 48000
CHANNELS = 2


def decode(path: str | Path) -> np.ndarray:
    """Archivo de audio -> (n, 2) float32 a 48 kHz. Levanta AudioError con un mensaje claro si no se puede leer."""
    try:
        r = subprocess.run([find_ffmpeg(), "-v", "error", "-i", str(path), "-vn", "-f", "f32le", "-ac", str(CHANNELS),
                            "-ar", str(RATE), "-"], capture_output=True, creationflags=NO_WINDOW, timeout=600)
    except FileNotFoundError as e:
        raise AudioError(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise AudioError("El archivo de audio tardó demasiado en leerse. Prueba con un archivo más corto.") from e
    if r.returncode != 0 or not r.stdout:
        raise AudioError("No pude leer ese archivo de audio. Usa un MP3, WAV, FLAC, M4A u OGG válido.")
    return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, CHANNELS).copy()


def waveform(samples: np.ndarray, bins: int) -> np.ndarray:
    """Pico absoluto por segmento (0..1) para dibujar la forma de onda."""
    mono = np.abs(samples).max(axis=1) if samples.ndim > 1 else np.abs(samples)
    if len(mono) == 0:
        return np.zeros(bins, np.float32)
    edges = np.linspace(0, len(mono), bins + 1, dtype=int)
    return np.array([mono[a:b].max() if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])], np.float32)


def segment(samples: np.ndarray, start_ms: float, end_ms: float, offset_ms: float = 0.0, fade_ms: float = 150.0) -> np.ndarray:
    """Trozo [start, end] de la canción. offset_ms > 0 = el audio del archivo va retrasado respecto a la letra
    (se lee más adelante en el archivo). Fuera del archivo rellena con silencio."""
    a = round((start_ms + offset_ms) / 1000 * RATE)
    n = round((end_ms - start_ms) / 1000 * RATE)
    out = np.zeros((n, samples.shape[1]), np.float32)
    lo, hi = max(a, 0), min(a + n, len(samples))
    if hi > lo:
        out[lo - a:hi - a] = samples[lo:hi]
    return apply_fades(out, RATE, fade_ms)


def write_wav(path: str | Path, samples: np.ndarray, rate: int = RATE):
    """WAV de 16 bits para pasárselo a ffmpeg (sin dependencias extra)."""
    import wave
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(samples.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
