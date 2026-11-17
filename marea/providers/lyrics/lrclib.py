"""LRCLIB: letras públicas, sin llave. Sincronía por línea (LRC) y, cuando existe, por palabra (lyricsfile)."""
import hashlib
from pathlib import Path

import httpx

from marea.i18n import t
from marea.model.lyrics import Lyrics, parse_lrc, parse_lyricsfile
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.http_cache import ThrottledHttpCache

BASE = "https://lrclib.net/api"
USER_AGENT = "ZH3 Marea/0.1"  # LRCLIB pide identificar la app
FOUND_TTL = 30 * 86400  # una letra encontrada se reutiliza 30 días
MISS_TTL = 86400  # «no hay letra» se recuerda un día (para no insistir)
MIN_INTERVAL = 0.25  # s entre peticiones a la red
DURATION_TOLERANCE_MS = 3000


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
            ly.source, ly.credit = "lrclib", "LRCLIB"
        return ly

    # ---- red y caché ----
    def _get(self, path: str, params: dict) -> httpx.Response:
        r = self._client.get(f"{BASE}{path}", params=params, service_name="LRCLIB")
        if r.status_code >= 500 or r.status_code == 429:
            raise ProviderError(t("errors.lyrics.overloaded", service="LRCLIB"))
        return r

    def _cache_path(self, q: TrackQuery) -> Path:
        key = f"{q.artist.lower()}|{q.title.lower()}|{q.album.lower()}|{round((q.duration_ms or 0) / 1000)}"
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

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
            found = self._get("/search", {"track_name": q.title, "artist_name": q.artist})
            try:
                results = found.json() if found.status_code == 200 else []
            except ValueError:
                results = []
            raw = self._best(results if isinstance(results, list) else [], q)
        self._client.write_cache(path, raw)
        return raw

    @staticmethod
    def _best(results: list, q: TrackQuery) -> dict | None:
        ok = [x for x in results if x.get("syncedLyrics") or x.get("hasWordSync")]
        if q.duration_ms:
            ok = [x for x in ok if abs((x.get("duration") or 0) * 1000 - q.duration_ms) <= DURATION_TOLERANCE_MS]
            ok.sort(key=lambda x: abs((x.get("duration") or 0) * 1000 - q.duration_ms))
        return ok[0] if ok else None

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
