from dataclasses import dataclass


class ProviderError(Exception):
    """Fallo de un proveedor (Spotify, LRCLIB…). El mensaje está escrito para mostrárselo al usuario: qué pasó y cómo arreglarlo."""


@dataclass
class PositionSample:
    """Single playback-position observation bracketed by local monotonic clock timestamps."""
    progress_ms: float
    t_sent: float
    t_recv: float

    @property
    def rtt(self) -> float:
        return self.t_recv - self.t_sent

    @property
    def c(self) -> float:
        """Model offset `position - local_clock` (seconds) at the round-trip midpoint."""
        return self.progress_ms / 1000 - (self.t_sent + self.t_recv) / 2
