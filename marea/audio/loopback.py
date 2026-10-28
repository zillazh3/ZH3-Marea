"""Captura de audio del equipo (WASAPI loopback o una entrada normal) con una línea de tiempo en reloj monotónico."""
import threading
import time
from dataclasses import dataclass

import numpy as np

from marea.providers.errors import ProviderError

LOOPBACK_SUFFIX = " [Loopback]"
GAP_S = 0.02  # un hueco mayor entre bloques se rellena con silencio; menos es jitter y se ignora


class AudioError(ProviderError):
    """Fallo de captura/decodificación; el mensaje es para el usuario."""


@dataclass
class Recording:
    samples: np.ndarray  # (n, canales) float32 en [-1, 1]
    rate: int
    t0: float  # time.perf_counter() del instante al que corresponde samples[0]

    @property
    def duration(self) -> float:
        return len(self.samples) / self.rate

    def time_of(self, index: int) -> float:
        return self.t0 + index / self.rate


def assemble_chunks(chunks: list[tuple[float, np.ndarray]], rate: int, channels: int, fallback_t0: float = 0.0) -> Recording:
    """Une bloques (instante de llegada, datos) en una Recording con tiempo real.

    WASAPI loopback no entrega paquetes mientras el dispositivo está en silencio: unir los bloques sin más movería
    el audio al inicio. Cada bloque se coloca según cuándo llegó, y los huecos se rellenan con ceros."""
    if not chunks:
        return Recording(np.zeros((0, channels), np.float32), rate, fallback_t0)
    t0 = chunks[0][0] - len(chunks[0][1]) / rate
    parts, pos = [], 0
    for arrival, data in chunks:
        start = round((arrival - len(data) / rate - t0) * rate)
        if start - pos > GAP_S * rate:
            parts.append(np.zeros((start - pos, channels), np.float32))
            pos = start
        parts.append(data)
        pos += len(data)
    return Recording(np.concatenate(parts), rate, t0)


def list_capture_devices() -> list[dict]:
    """Dispositivos de los que se puede capturar: loopbacks de salidas y entradas WASAPI normales.
    [{'name', 'loopback': bool, 'rate', 'channels'}]. Las entradas sirven p. ej. para las salidas B1/B2 de Voicemeeter."""
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        out = [{"name": d["name"], "loopback": True, "rate": int(d["defaultSampleRate"]), "channels": d["maxInputChannels"]}
               for d in p.get_loopback_device_info_generator()]
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] == wasapi and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
                out.append({"name": d["name"], "loopback": False, "rate": int(d["defaultSampleRate"]),
                            "channels": d["maxInputChannels"]})
        return out
    finally:
        p.terminate()


def probe_level(device_name: str = "", seconds: float = 1.5) -> float:
    """Pico (0..1) de lo que suena en el dispositivo durante `seconds`. No reproduce nada: sirve para comprobar que la captura oye."""
    rec = LoopbackRecorder(device_name)
    rec.start()
    time.sleep(seconds)
    r = rec.stop()
    return float(np.abs(r.samples).max()) if r.samples.size else 0.0


def default_loopback_name() -> str:
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        return p.get_default_wasapi_loopback()["name"]
    except (OSError, LookupError) as e:
        raise AudioError("No encontré un dispositivo de audio para capturar. Revisa que tengas una salida de audio activa.") from e
    finally:
        p.terminate()


class LoopbackRecorder:
    """start() empieza a acumular; stop() devuelve la Recording. Pensado para un solo uso."""

    def __init__(self, device_name: str = ""):
        self.device_name = device_name  # vacío = el loopback de la salida por defecto de Windows
        self._chunks: list[tuple[float, np.ndarray]] = []
        self._pa = self._stream = None
        self._lock = threading.Lock()
        self.rate, self.channels, self.started, self.overflows = 0, 2, 0.0, 0

    def start(self):
        import pyaudiowpatch as pa
        self._pa = pa.PyAudio()
        try:
            dev = self._find_device()
            self.rate, self.channels = int(dev["defaultSampleRate"]), dev["maxInputChannels"]
            self._stream = self._pa.open(format=pa.paFloat32, channels=self.channels, rate=self.rate, input=True,
                                         input_device_index=dev["index"], frames_per_buffer=512,
                                         stream_callback=self._callback)
        except OSError as e:
            self._pa.terminate()
            raise AudioError(f"No pude abrir «{self.device_name or 'el dispositivo de audio'}» para capturar. Revisa que esté "
                             "conectado y activo, o elige otro en Ajustes > Audio.") from e
        self.started = time.perf_counter()
        self._stream.start_stream()

    def _find_device(self) -> dict:
        if not self.device_name:
            try:
                return self._pa.get_default_wasapi_loopback()
            except (OSError, LookupError) as e:
                raise AudioError("No encontré un dispositivo de audio para capturar. Revisa que tengas una salida de audio activa.") from e
        import pyaudiowpatch as pa
        wasapi = self._pa.get_host_api_info_by_type(pa.paWASAPI)["index"]
        loop = list(self._pa.get_loopback_device_info_generator())
        for d in loop:
            if d["name"] == self.device_name:
                return d
        for i in range(self._pa.get_device_count()):
            d = self._pa.get_device_info_by_index(i)
            if d["hostApi"] == wasapi and d["maxInputChannels"] > 0 and d["name"] == self.device_name:
                return d
        raise AudioError(f"No encontré el dispositivo «{self.device_name}». Elige otro en Ajustes > Audio.")

    def _callback(self, data, frames, time_info, status):
        import pyaudiowpatch as pa
        arrival = time.perf_counter()
        if status:
            self.overflows += 1
        chunk = np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels).copy()
        with self._lock:
            self._chunks.append((arrival, chunk))
        return (None, pa.paContinue)

    def stop(self) -> Recording:
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        finally:
            if self._pa:
                self._pa.terminate()
        with self._lock:
            chunks = list(self._chunks)
        return assemble_chunks(chunks, self.rate or 48000, self.channels, self.started)


def scan_levels(seconds: float = 1.5, devices: list[dict] | None = None, recorder_factory=LoopbackRecorder,
                sleep=time.sleep) -> list[dict]:
    """Abre todos los dispositivos de captura a la vez y mide el pico de cada uno: [{**dispositivo, 'peak'}], del que
    más suena al que menos. Con Spotify sonando, el que tiene señal es por donde sale su audio."""
    # ponytail: por dispositivo, no por proceso (WASAPI process-loopback no está en PyAudioWPatch); sirve mientras solo suene Spotify
    started, out = [], []
    try:
        for d in list_capture_devices() if devices is None else devices:
            rec = recorder_factory(d["name"])
            try:
                rec.start()
            except AudioError:
                continue  # ocupado o desconectado: no puede ser la fuente
            started.append((d, rec))
        sleep(seconds)
    finally:  # aunque algo falle, ningún dispositivo se queda abierto
        for d, rec in started:
            try:
                r = rec.stop()
            except OSError:
                continue
            out.append({**d, "peak": float(np.abs(r.samples).max()) if r.samples.size else 0.0})
    return sorted(out, key=lambda d: -d["peak"])
