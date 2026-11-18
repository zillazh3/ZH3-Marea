"""Ordered lyrics provider chain: Spicy Lyrics primary -> LRCLIB fallback, with sync validation."""
import math

from marea.model.lyrics import SOURCE_LRCLIB, SOURCE_SPICY, Lyrics, resolve_source_tag
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import LyricsProvider, TrackQuery


def validate_lyrics(lyrics: Lyrics | None) -> bool:
    """Validate that lyrics contain non-empty lines with finite, non-negative, ordered synchronization timing."""
    if lyrics is None or not lyrics.lines:
        return False
    for line in lyrics.lines:
        if not (isinstance(line.start, (int, float)) and isinstance(line.end, (int, float))):
            return False
        if not (math.isfinite(line.start) and math.isfinite(line.end)):
            return False
        if line.start < 0 or line.end < line.start:
            return False
        if not line.syllables and not line.bg:
            return False
        if not line.text.strip() and not line.bg:
            return False
        for syl in line.syllables:
            if syl.begin < 0 or syl.end < syl.begin:
                return False
        for bg in line.bg:
            if bg.start < 0 or bg.end < bg.start or not bg.syllables:
                return False
    return True


def _tag_for_provider(provider: LyricsProvider, lyrics: Lyrics) -> str:
    pname = getattr(provider, "name", "")
    if pname == "spicy_lyrics":
        return SOURCE_SPICY
    if pname == "lrclib":
        return SOURCE_LRCLIB
    return resolve_source_tag(lyrics.source, lyrics.provider_tag, manual_override=lyrics.manual_override)


class ChainProvider:
    name = "chain"

    def __init__(self, *providers: LyricsProvider):
        self.providers = providers

    def fetch(self, query: TrackQuery | None = None) -> Lyrics | None:
        """Try providers in priority order; fall back only when primary returns no valid synchronized lyrics."""
        error: ProviderError | None = None
        for p in self.providers:
            try:
                ly = p.fetch(query)
            except ProviderError as e:
                error = error or e
                continue
            if validate_lyrics(ly):
                ly.provider_tag = _tag_for_provider(p, ly)
                return ly
        if error:
            raise error
        return None
