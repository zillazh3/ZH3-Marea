"""Varios proveedores en orden de preferencia: gana el primero con sílabas reales; si ninguno las tiene, el primero con letra por línea."""
from marea.model.lyrics import Lyrics
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import LyricsProvider, TrackQuery


class ChainProvider:
    name = "chain"

    def __init__(self, *providers: LyricsProvider):
        self.providers = providers

    def fetch(self, query: TrackQuery | None = None) -> Lyrics | None:
        """Un proveedor que falla (sin red, llave mala, límite) no impide que otro responda; el error solo sale si nadie dio letra."""
        best: Lyrics | None = None
        error: ProviderError | None = None
        for p in self.providers:
            try:
                ly = p.fetch(query)
            except ProviderError as e:
                error = error or e
                continue
            if ly and ly.kind == "syllable":
                return ly
            best = best or ly  # letra por línea: se sigue buscando por si otro proveedor la tiene con sílabas
        if best is None and error:
            raise error
        return best
