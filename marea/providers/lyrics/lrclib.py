"""LRCLIB: letras públicas, sin llave. Sincronía por línea (LRC) y, cuando existe, por palabra (lyricsfile)."""
import hashlib
from pathlib import Path

import httpx

from marea.i18n import t
from marea.model.lyrics import SOURCE_LRCLIB, Lyrics, parse_lrc, parse_lyricsfile
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.http_cache import ThrottledHttpCache
from marea.providers.matching import score_track, split_artists, split_title_and_metadata

BASE = "https://lrclib.net/api"
USER_AGENT = "ZH3 Marea/0.1"  # LRCLIB pide identificar la app
FOUND_TTL = 30 * 86400  # una letra encontrada se reutiliza 30 días
MISS_TTL = 86400  # «no hay letra» se recuerda un día (para no insistir)
MIN_INTERVAL = 0.25  # s entre peticiones a la red
DURATION_TOLERANCE_MS = 3000
MAX_FALLBACK_DURATION_MS = 8000


class LrclibProvider:
    name = "lrclib"

    def __init__(self, http: httpx.Client | None = None, cache_dir: Path | None = None):
        self._client = ThrottledHttpCache(
            "lrclib", USER_AGENT, http=http, cache_dir=cache_dir,
            min_interval=MIN_INTERVAL, found_ttl=FOUND_TTL, miss_ttl=MISS_TTL,
        )
        self.http = self._client.http
        self.cache_dir = self._client.cache_dir

    def close(self):
        self._client.close()

    # ---- API pública ----
    def fetch(self, query: TrackQuery | None = None) -> Lyrics | None:
        """Letra sincronizada o None. Levanta ProviderError si LRCLIB no responde."""
        if not query or not query.title or not query.artist:
            return None  # LRCLIB identifica por título y artista
        raw = self._lookup(query)
        ly = self._to_lyrics(raw) if raw else None
        if ly:
            ly.source, ly.credit, ly.provider_tag = "lrclib", "LRCLIB", SOURCE_LRCLIB
        return ly

    def search_candidates(self, query: TrackQuery, limit: int = 8) -> list[tuple[dict, Lyrics]]:
        """Return ranked synced LRCLIB candidates `(raw_item, parsed_lyrics)` for manual disambiguation."""
        if not query or not query.title:
            return []
        results = self._search_raw(query)
        ranked = self._rank_results(results, query)
        out: list[tuple[dict, Lyrics]] = []
        for item, _ in ranked[:limit]:
            ly = self._to_lyrics(item)
            if ly:
                ly.source, ly.credit, ly.provider_tag = "lrclib", "LRCLIB", SOURCE_LRCLIB
                out.append((item, ly))
        return out

    # ---- red y caché ----
    def _get(self, path: str, params: dict) -> httpx.Response:
        r = self._client.get(f"{BASE}{path}", params=params, service_name="LRCLIB")
        if r.status_code >= 500 or r.status_code == 429:
            raise ProviderError(t("errors.lyrics.overloaded", service="LRCLIB"))
        return r

    def _cache_path(self, q: TrackQuery) -> Path:
        key = f"{q.artist.lower()}|{q.title.lower()}|{q.album.lower()}|{round((q.duration_ms or 0) / 1000)}"
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _search_raw(self, q: TrackQuery) -> list:
        params = {"track_name": q.title, "artist_name": q.artist} if q.artist else {"q": q.title}
        found = self._get("/search", params)
        try:
            results = found.json() if found.status_code == 200 else []
        except ValueError:
            results = []
        if not results:
            base_title, _, _ = split_title_and_metadata(q.title)
            artists = split_artists(q.artist)
            clean_params = {"track_name": base_title or q.title}
            if artists:
                clean_params["artist_name"] = artists[0]
            if clean_params != params:
                found2 = self._get("/search", clean_params)
                try:
                    results = found2.json() if found2.status_code == 200 else []
                except ValueError:
                    results = []
        return results if isinstance(results, list) else []

    def _lookup(self, q: TrackQuery) -> dict | None:
        path = self._cache_path(q)
        hit, cached = self._client.read_cache(path)
        if hit:
            return cached
        params = {"track_name": q.title, "artist_name": q.artist}
        if q.album:
            params["album_name"] = q.album
        if q.duration_ms:
            params["duration"] = round(q.duration_ms / 1000)
        r = self._get("/get", params)
        try:
            raw = r.json() if r.status_code == 200 else None
        except ValueError:
            raw = None
        if raw is None:  # /get exige coincidencia casi exacta; /search es más tolerante
            raw = self._best(self._search_raw(q), q)
        self._client.write_cache(path, raw)
        return raw

    @staticmethod
    def _rank_results(results: list, q: TrackQuery) -> list[tuple[dict, float]]:
        ok = [x for x in results if isinstance(x, dict) and not x.get("instrumental")
              and (x.get("syncedLyrics") or x.get("hasWordSync"))]
        if q.duration_ms:
            tight = [x for x in ok if abs((x.get("duration") or 0) * 1000 - q.duration_ms) <= DURATION_TOLERANCE_MS]
            ok = tight or [x for x in ok if abs((x.get("duration") or 0) * 1000 - q.duration_ms) <= MAX_FALLBACK_DURATION_MS]
        scored: list[tuple[dict, float]] = []
        for x in ok:
            dur_ms = round((x.get("duration") or 0) * 1000) or None
            cand_title = str(x.get("trackName") or x.get("name") or q.title)
            cand_artist = str(x.get("artistName") or q.artist)
            m = score_track(
                query_title=q.title,
                query_artist=q.artist,
                query_duration_ms=q.duration_ms,
                cand_title=cand_title,
                cand_artists=cand_artist,
                cand_duration_ms=dur_ms,
            )
            word_bonus = 0.03 if x.get("hasWordSync") else 0.0
            scored.append((x, m.score + word_bonus))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    @classmethod
    def _best(cls, results: list, q: TrackQuery) -> dict | None:
        ranked = cls._rank_results(results, q)
        if not ranked:
            return None
        best_item, best_score = ranked[0]
        return best_item if best_score >= 0.45 else None

    @staticmethod
    def _to_lyrics(raw: dict) -> Lyrics | None:
        if raw.get("instrumental"):
            return None
        if raw.get("hasWordSync") and raw.get("lyricsfile"):
            try:
                ly = parse_lyricsfile(raw["lyricsfile"])
                if ly.lines:
                    return ly
            except ValueError:
                pass  # lyricsfile roto o de otra versión: se usa el LRC
        ly = parse_lrc(raw.get("syncedLyrics") or "")
        return ly if ly.lines else None
