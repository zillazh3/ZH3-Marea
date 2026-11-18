from pathlib import Path

from marea.model.lyrics import SOURCE_MANUAL, Lyrics, parse_ttml
from marea.providers.lyrics.base import TrackQuery


class TtmlFileProvider:
    """Importa un .ttml local (sílaba por sílaba); ignora la consulta: la letra es la del archivo."""

    name = "ttml_file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self, query: TrackQuery | None = None) -> Lyrics:
        ly = parse_ttml(self.path.read_bytes())
        ly.provider_tag = SOURCE_MANUAL
        ly.manual_override = True
        return ly
