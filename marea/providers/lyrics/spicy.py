"""Spicy Lyrics (API para desarrolladores): letra con sílabas reales, de Apple Music o de la comunidad, por id de pista de Spotify.

GET /v1/lyrics/{id} con `Authorization: Bearer sl_pk_…` (llave de cliente: se saca en developers.spicylyrics.org y, al no mandar la app
un Origin, hay que activar «Allow requests with no Origin header»). La respuesta trae `Body.Type` = Syllable | Line | Static y
`Body.source` = apple_music | spicy_lyrics | spotify; los tiempos vienen en segundos. Cada respuesta debe mostrar su crédito (`Lyrics.credit`).
"""
import re
import time
from pathlib import Path
from typing import Callable

import httpx

from marea.i18n import t
from marea.model.lyrics import BgVocal, Line, Lyrics, Syllable, spread
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.http_cache import ThrottledHttpCache

BASE = "https://api.spicylyrics.org/v1"
USER_AGENT = "ZH3 Marea/0.1"
FOUND_TTL = 7 * 86400  # una letra encontrada se reutiliza una semana (la API permite hasta 30 días)
MISS_TTL = 86400  # «sin letra» se recuerda un día
MIN_INTERVAL = 0.25  # s entre peticiones a la red
PROBE_ID = "7KNmIjcmGJIBrhP2s5Vioe"  # pista cualquiera con letra: sirve para comprobar que la llave es aceptada
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
SOURCES = {"apple_music": "Apple Music", "spicy_lyrics": "Spicy Lyrics", "spotify": "Spotify"}


class SpicyKeyError(ProviderError):
    """La llave falta o no es aceptada: el usuario puede arreglarlo en Ajustes."""


def _ms(seconds) -> int:
    return round(float(seconds) * 1000)


def _syllables(raw: list) -> list[Syllable]:
    """Las sílabas llegan sin espacios; `IsPartOfWord` = se pega con la siguiente («h-heartbreak», «Mum-mum-mum-mah»)."""
    out: list[Syllable] = []
    for s in raw:
        text = str(s.get("Text", "")).strip()
        if not text:
            if out:
                out[-1].word_end = True  # un texto en blanco separa palabras
            continue
        b = _ms(s["StartTime"])
        out.append(Syllable(text, b, max(_ms(s["EndTime"]), b), word_end=not s.get("IsPartOfWord")))
    if out:
        out[-1].word_end = False
    return out


def _syllable_line(item: dict) -> Line | None:
    lead = item.get("Lead") or {}
    syls = _syllables(lead.get("Syllables") or [])
    if not syls:
        return None
    bg = []
    for g in item.get("Background") or []:
        bs = _syllables(g.get("Syllables") or [])
        if bs:
            b0 = _ms(g["StartTime"]) if g.get("StartTime") is not None else bs[0].begin
            b1 = _ms(g["EndTime"]) if g.get("EndTime") is not None else bs[-1].end
            bg.append(BgVocal(min(b0, bs[0].begin), max(b1, bs[-1].end), bs))
    start = min(_ms(lead["StartTime"]), syls[0].begin) if lead.get("StartTime") is not None else syls[0].begin
    end = max([syls[-1].end, _ms(lead["EndTime"]) if lead.get("EndTime") is not None else 0] + [b.end for b in bg])
    return Line(start, end, "v2" if item.get("OppositeAligned") else "v1", syls, bg)


def _name(raw) -> str:
    """Los nombres los escribe un tercero (en datos reales hay «makers» de 100+ caracteres): sin control/bidi y con tope."""
    s = "".join(c for c in str(raw or "") if c.isprintable()).strip()
    return s if len(s) <= 32 else s[:31] + "…"


def _credit(body: dict) -> str:
    """Quién proporciona la letra (para «Provided by …»); en las de la comunidad, también quién la subió y quién la hizo."""
    src = body.get("source")
    if src == "spicy_lyrics":
        att = body.get("UploadAttribution") or {}
        up, mk = (_name((att.get(k) or {}).get("username")) for k in ("Uploader", "Maker"))
        who = ", ".join(p for p in (f"uploaded by {up}" if up else "", f"made by {mk}" if mk else "") if p)
        return "Spicy Lyrics" + (f" · {who}" if who else "")
    return SOURCES.get(src, "Spicy Lyrics")


def _writers(body: dict) -> list[str]:
    raw = body.get("SongWriters")
    return [n for n in (_name(w) for w in raw[:12]) if n] if isinstance(raw, list) else []


class SpicyLyricsProvider:
    name = "spicy_lyrics"

    def __init__(self, key: Callable[[], str], http: httpx.Client | None = None, cache_dir: Path | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        """`key()` se consulta en cada petición: si el usuario cambia la llave en Ajustes, aplica sin reiniciar."""
        self.key = key
        self._client = ThrottledHttpCache(
            "spicy", USER_AGENT, http=http, cache_dir=cache_dir,
            min_interval=MIN_INTERVAL, found_ttl=FOUND_TTL, miss_ttl=MISS_TTL,
            clock=clock, sleep=sleep,
        )
        self.http = self._client.http
        self.cache_dir = self._client.cache_dir

    def close(self):
        self._client.close()

    # ---- API pública ----
    def fetch(self, query: TrackQuery | None = None) -> Lyrics | None:
        """Letra con sílabas (o por línea) o None. Sin llave o sin id de Spotify no hay nada que pedir. Levanta ProviderError si falla."""
        key = self.key().strip()
        if not key or not query or not SPOTIFY_ID.fullmatch(query.spotify_id or ""):
            return None
        raw = self._lookup(query.spotify_id, key)
        return self._to_lyrics(raw) if raw else None

    def check_key(self):
        """Pide una pista conocida para comprobar la llave. Levanta SpicyKeyError si no sirve (otros ProviderError: red, límite)."""
        key = self.key().strip()
        if not key:
            raise SpicyKeyError(t("errors.lyrics.spicyKeyMissing"))
        self._get(PROBE_ID, key)  # 200 y 404 significan que la llave pasó; el resto levanta

    # ---- red y caché ----
    def _get(self, track_id: str, key: str) -> httpx.Response:
        r = self._client.get(
            f"{BASE}/lyrics/{track_id}",
            headers={"Authorization": f"Bearer {key}"},
            service_name="Spicy Lyrics",
            on_response=self._note_limits,
        )
        if r.status_code == 429:
            raise ProviderError(t("errors.lyrics.spicyRateLimited", wait=f"{self._wait(r):.0f}"))
        if r.status_code in (401, 403):
            raise SpicyKeyError(t("errors.lyrics.spicyKeyHelp"))
        if r.status_code >= 500:
            raise ProviderError(t("errors.lyrics.overloaded", service="Spicy Lyrics"))
        return r

    @staticmethod
    def _wait(r: httpx.Response) -> float:
        for h in ("retry-after", "ratelimit-reset"):
            try:
                return max(1.0, float(r.headers[h]))
            except (KeyError, ValueError):
                pass
        return 10.0

    def _note_limits(self, r: httpx.Response):
        """El servidor informa cuántas peticiones quedan en la ventana; al agotarlas, se deja de consultar hasta que se renueve."""
        if r.status_code == 429 or r.headers.get("ratelimit-remaining") == "0":
            self._client.block_for(self._wait(r))

    def _cache_path(self, track_id: str) -> Path:
        return self.cache_dir / f"{track_id}.json"

    def _lookup(self, track_id: str, key: str) -> dict | None:
        path = self._cache_path(track_id)
        hit, cached = self._client.read_cache(path)
        if hit:
            return cached
        r = self._get(track_id, key)
        try:
            raw = r.json() if r.status_code == 200 else None
        except ValueError:
            raw = None
        self._client.write_cache(path, raw)
        return raw

    # ---- respuesta -> modelo ----
    @staticmethod
    def _to_lyrics(raw: dict) -> Lyrics | None:
        body = raw.get("Body") if isinstance(raw, dict) else None
        if not isinstance(body, dict) or body.get("Type") not in ("Syllable", "Line"):
            return None  # Static no tiene tiempos: otro proveedor (LRCLIB) puede dar letra sincronizada
        items = [i for i in body.get("Content") or [] if isinstance(i, dict)]
        lines = []
        for n, item in enumerate(items):
            try:
                line = _syllable_line(item) if body["Type"] == "Syllable" or "Lead" in item else SpicyLyricsProvider._text_line(item, items[n + 1:])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # un verso mal formado no tira la letra entera
            if line:
                lines.append(line)
        if not lines:
            return None
        lines.sort(key=lambda l: l.start)
        return Lyrics(lines, source=str(body.get("source") or ""), credit=_credit(body), writers=_writers(body))

    @staticmethod
    def _text_line(item: dict, following: list) -> Line | None:
        """Letra sincronizada solo por línea: `Text` + `StartTime`/`EndTime`; los tiempos por palabra se reparten por letras."""
        text = " ".join(str(item.get("Text", "")).split())
        if not text or item.get("StartTime") is None:
            return None
        start = _ms(item["StartTime"])
        nxt = next((_ms(i["StartTime"]) for i in following if i.get("StartTime") is not None), None)
        end = _ms(item["EndTime"]) if item.get("EndTime") is not None else (nxt if nxt and nxt > start else start + 5000)
        end = max(end, start + 1)
        return Line(start, end, "v2" if item.get("OppositeAligned") else "v1", spread(text, start, end), approx=True)
