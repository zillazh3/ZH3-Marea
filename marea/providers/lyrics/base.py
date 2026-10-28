from dataclasses import dataclass
from typing import Protocol

from marea.model.lyrics import Lyrics


@dataclass
class TrackQuery:
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    spotify_id: str = ""  # Spicy Lyrics identifica por id de pista de Spotify


class LyricsProvider(Protocol):
    name: str

    def fetch(self, query: TrackQuery | None = None) -> Lyrics | None:
        """Letra de la canción, o None si el proveedor no la tiene."""
