"""LRCLIB: letras públicas, sin llave. Sincronía por línea (LRC) y, cuando existe, por palabra (lyricsfile)."""
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx

from marea.model.lyrics import Lyrics, parse_lrc, parse_lyricsfile
from marea.paths import app_data
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import TrackQuery

BASE = "https://lrclib.net/api"
USER_AGENT = "ZH3 Marea/0.1"  # LRCLIB pide identificar la app
FOUND_TTL = 30 * 86400  # una letra encontrada se reutiliza 30 días
MISS_TTL = 86400  # «no hay letra» se recuerda un día (para no insistir)
MIN_INTERVAL = 0.25  # s entre peticiones a la red
DURATION_TOLERANCE_MS = 3000


class LrclibProvider:
    name = "lrclib"

    def __init__(self, http: httpx.Client | None = None, cache_dir: Path | None = None):
        self.http = http or httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT})
        self.cache_dir = cache_dir if cache_dir is not None else app_data() / "cache" / "lrclib"
        self._lock, self._last = threading.Lock(), 0.0

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
        with self._lock:  # un hilo a la vez y con pausa mínima: no abusar del servicio
            time.sleep(max(0.0, self._last + MIN_INTERVAL - time.monotonic()))
            try:
                r = self.http.get(f"{BASE}{path}", params=params)
            except httpx.HTTPError as e:
                raise ProviderError("LRCLIB no respondió. Revisa tu conexión y vuelve a intentar.") from e
            finally:
                self._last = time.monotonic()
        if r.status_code >= 500 or r.status_code == 429:
            raise ProviderError("LRCLIB está saturado o caído en este momento. Intenta de nuevo en unos minutos.")
        return r

    def _cache_path(self, q: TrackQuery) -> Path:
        key = f"{q.artist.lower()}|{q.title.lower()}|{q.album.lower()}|{round((q.duration_ms or 0) / 1000)}"
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _lookup(self, q: TrackQuery) -> dict | None:
        path = self._cache_path(q)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - cached["t"] < (FOUND_TTL if cached["data"] else MISS_TTL):
                return cached["data"]
        except (OSError, ValueError, KeyError):
            pass
        params = {"track_name": q.title, "artist_name": q.artist}
        if q.album:
            params["album_name"] = q.album
        if q.duration_ms:
            params["duration"] = round(q.duration_ms / 1000)
        r = self._get("/get", params)
        raw = r.json() if r.status_code == 200 else None
        if raw is None:  # /get exige coincidencia casi exacta; /search es más tolerante
            found = self._get("/search", {"track_name": q.title, "artist_name": q.artist})
            raw = self._best(found.json() if found.status_code == 200 else [], q)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"t": time.time(), "data": raw}), encoding="utf-8")
        except OSError:
            pass  # sin caché también funciona
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
