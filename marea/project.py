"""Proyecto de clip: la letra, los datos de la canción y su portada."""
from dataclasses import dataclass
from pathlib import Path

import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.model.lyrics import Lyrics
from marea.providers.lyrics.ttml_file import TtmlFileProvider
from marea.providers.spotify import Track


@dataclass
class ClipProject:
    lyrics: Lyrics
    title: str
    artist: str
    cover: skia.Image
    source: Path | None = None
    track: Track | None = None  # canción de Spotify de la que viene (permite grabar su audio por loopback)
    audio_file: Path | None = None  # audio propio elegido por el usuario
    audio_offset_ms: float = 0.0  # >0 = el audio del archivo va retrasado respecto a la letra
    audio_samples: object = None  # (n, 2) float32 ya decodificado; solo en memoria

    def cover_png(self) -> bytes:
        return bytes(self.cover.encodeToData())  # PNG; viaja al proceso de export


def demo_project() -> ClipProject:
    return ClipProject(exact_lyrics(), "Marea de luz", "Demo · letra original", demo_cover())


def project_from_ttml(path: str | Path) -> ClipProject:
    """Abre un .ttml. Levanta ValueError (mensaje claro) si no es válido; sin portada usa una de relleno."""
    path = Path(path)
    lyrics = TtmlFileProvider(path).fetch()
    if not lyrics.lines:
        raise ValueError("El archivo no tiene versos con tiempos.")
    return ClipProject(lyrics, path.stem, "", demo_cover(label=False), path)


def load_cover(path: str | Path) -> skia.Image:
    img = skia.Image.open(str(path))
    if img is None:
        raise ValueError("No pude abrir esa imagen. Usa un PNG, JPG o WebP.")
    return img
