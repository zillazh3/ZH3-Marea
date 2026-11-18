"""Deezer public catalog client: search fallback, track lookup, artwork download, and TTL disk cache."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

import httpx

from marea.i18n import t
from marea.providers.errors import ProviderError
from marea.providers.lyrics.http_cache import ThrottledHttpCache
from marea.providers.matching import RankedTrack, clean_search_query, rank_tracks
from marea.providers.track import Track

BASE = "https://api.deezer.com"
USER_AGENT = "ZH3 Marea/0.2"
FOUND_TTL = 7 * 86400
MISS_TTL = 3600
MIN_INTERVAL = 0.12
SEARCH_LIMIT = 15
_DEEZER_ID = re.compile(r"\d+")


class DeezerError(ProviderError):
    pass


class DeezerClient:
    """Public Deezer Web API client (no authentication required)."""

    name = "deezer"

    def __init__(self, http: httpx.Client | None = None, cache_dir: Path | None = None):
        self._client = ThrottledHttpCache(
            "deezer",
            USER_AGENT,
            http=http,
            cache_dir=cache_dir,
            min_interval=MIN_INTERVAL,
            found_ttl=FOUND_TTL,
            miss_ttl=MISS_TTL,
        )
        self.http = self._client.http
        self.cache_dir = self._client.cache_dir

    def close(self):
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        r = self._client.get(f"{BASE}{path}", params=params, service_name="Deezer")
        if r.status_code == 429:
            self._client.block_for(5.0)
            raise DeezerError(t("errors.lyrics.waitRequested", service="Deezer", wait=5))
        if r.status_code >= 500:
            raise DeezerError(t("errors.lyrics.overloaded", service="Deezer"))
        if r.status_code == 404:
            return None
        try:
            data = r.json()
        except ValueError as e:
            raise DeezerError(t("errors.lyrics.noResponse", service="Deezer")) from e
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            if code == 4:  # Deezer QuotaException
                self._client.block_for(5.0)
                raise DeezerError(t("errors.lyrics.waitRequested", service="Deezer", wait=5))
            if code == 800:  # DATA_NOT_FOUND
                return None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise DeezerError(str(msg or t("errors.lyrics.noResponse", service="Deezer")))
        return data if isinstance(data, dict) else None

    def track(self, track_id: str) -> Track | None:
        tid = str(track_id or "").strip()
        if not _DEEZER_ID.fullmatch(tid):
            return None
        cpath = self.cache_dir / f"track_{tid}.json"
        hit, cached = self._client.read_cache(cpath)
        if hit:
            return Track.from_deezer(cached) if cached else None
        raw = self._get(f"/track/{tid}")
        if raw and not raw.get("id"):
            raw = None
        self._client.write_cache(cpath, raw)
        return Track.from_deezer(raw) if raw else None

    def _search_raw(self, q: str, limit: int) -> list[dict]:
        key = hashlib.sha1(f"{q.strip().lower()}|{limit}".encode()).hexdigest()
        cpath = self.cache_dir / f"search_{key}.json"
        hit, cached = self._client.read_cache(cpath)
        if hit:
            return list((cached or {}).get("data") or [])
        data = self._get("/search/track", {"q": q, "limit": limit})
        items = [i for i in (data or {}).get("data") or [] if isinstance(i, dict) and i.get("id")]
        self._client.write_cache(cpath, {"data": items} if items else None)
        return items

    def search_ranked(
        self,
        query: str = "",
        *,
        title: str = "",
        artist: str = "",
        duration_ms: int | None = None,
        limit: int = SEARCH_LIMIT,
    ) -> list[RankedTrack]:
        q_raw = query.strip() or f"{artist} {title}".strip()
        if not q_raw:
            return []

        items: list[dict] = []
        if title and artist:
            structured = f'artist:"{clean_search_query(artist)}" track:"{clean_search_query(title)}"'
            items = self._search_raw(structured, limit)
        if not items and q_raw:
            items = self._search_raw(q_raw, limit)
        if not items:
            cleaned = clean_search_query(q_raw)
            if cleaned and cleaned.lower() != q_raw.lower():
                items = self._search_raw(cleaned, limit)
        if not items and title:
            items = self._search_raw(clean_search_query(title), limit)

        seen: set[str] = set()
        tracks: list[Track] = []
        for item in items:
            t_obj = Track.from_deezer(item)
            if t_obj.id and t_obj.id not in seen:
                seen.add(t_obj.id)
                tracks.append(t_obj)

        return rank_tracks(
            tracks,
            query_text=query,
            query_title=title,
            query_artist=artist,
            query_duration_ms=duration_ms,
        )

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[Track]:
        return [r.track for r in self.search_ranked(query, limit=limit)]

    def cover(self, url: str) -> bytes:
        try:
            r = self.http.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise DeezerError(t("errors.spotify.coverDownload")) from e
        return r.content
