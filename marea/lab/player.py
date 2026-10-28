"""Reproducción de un tramo de la canción para escuchar desde una unidad en la revisión (PyAudioWPatch, ya parte de la app).

Solo se usa dentro de la página experimental: el preview del Editor sigue siendo mudo. El audio entra a 48 kHz estéreo (el formato de
`audio.local.decode`); si el dispositivo de salida trabaja a otra frecuencia, el tramo se remuestrea una vez (interpolación lineal) antes de empezar.
"""
import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from marea.audio.local import RATE


class Player(QObject):
    position = Signal(float)  # ms de la canción mientras suena
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pa = self._stream = None
        self._buf, self._pos, self._rate, self._start_ms = np.zeros((0, 2), np.float32), 0, RATE, 0.0
        self._timer = QTimer(self, interval=30)
        self._timer.timeout.connect(self._tick)

    @property
    def playing(self) -> bool:
        return self._stream is not None

    @property
    def position_ms(self) -> float:
        return self._start_ms + self._pos / self._rate * 1000

    def play(self, samples: np.ndarray, start_ms: float, end_ms: float | None = None):
        """Suena `samples[start_ms:end_ms]` (n×2, float32, 48 kHz). Levanta OSError si no hay dispositivo de salida."""
        self.stop()
        a = max(0, round(start_ms / 1000 * RATE))
        b = len(samples) if end_ms is None else min(len(samples), round(end_ms / 1000 * RATE))
        if b <= a:
            return
        import pyaudiowpatch as pyaudio

        self._pa = self._pa or pyaudio.PyAudio()
        info = self._pa.get_default_output_device_info()  # OSError si no hay salida
        rate = int(info["defaultSampleRate"])
        chunk = np.ascontiguousarray(samples[a:b], np.float32)
        if rate != RATE:
            x = np.linspace(0, len(chunk) - 1, round(len(chunk) * rate / RATE))
            chunk = np.stack([np.interp(x, np.arange(len(chunk)), chunk[:, c]) for c in range(2)], axis=1).astype(np.float32)
        self._buf, self._pos, self._rate, self._start_ms = chunk, 0, rate, a / RATE * 1000

        def callback(_in, frames, _time, _status):
            part = self._buf[self._pos:self._pos + frames]
            self._pos += len(part)
            if len(part) < frames:
                part = np.vstack([part, np.zeros((frames - len(part), 2), np.float32)])
                return part.tobytes(), pyaudio.paComplete
            return part.tobytes(), pyaudio.paContinue

        self._stream = self._pa.open(format=pyaudio.paFloat32, channels=2, rate=rate, output=True, output_device_index=info["index"],
                                     stream_callback=callback)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            finally:
                self._stream = None

    def _tick(self):
        if self._stream is None:
            return
        self.position.emit(self.position_ms)
        if not self._stream.is_active():
            self.stop()
            self.finished.emit()

    def close(self):
        self.stop()
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
