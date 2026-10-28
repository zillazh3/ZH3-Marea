"""Spicy Lyrics (API para desarrolladores): letra con sílabas reales, de Apple Music o de la comunidad, por id de pista de Spotify.

GET /v1/lyrics/{id} con `Authorization: Bearer sl_pk_…` (llave de cliente: se saca en developers.spicylyrics.org y, al no mandar la app
un Origin, hay que activar «Allow requests with no Origin header»). La respuesta trae `Body.Type` = Syllable | Line | Static y
`Body.source` = apple_music | spicy_lyrics | spotify; los tiempos vienen en segundos. Cada respuesta debe mostrar su crédito (`Lyrics.credit`).
"""
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from marea.model.lyrics import BgVocal, Line, Lyrics, Syllable, spread
from marea.paths import app_data
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import TrackQuery

BASE = "https://api.spicylyrics.org/v1"
USER_AGENT = "ZH3 Marea/0.1"
FOUND_TTL = 7 * 86400  # una letra encontrada se reutiliza una semana (la API permite hasta 30 días)
MISS_TTL = 86400  # «sin letra» se recuerda un día
MIN_INTERVAL = 0.25  # s entre peticiones a la red
PROBE_ID = "7KNmIjcmGJIBrhP2s5Vioe"  # pista cualquiera con letra: sirve para comprobar que la llave es aceptada
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
SOURCES = {"apple_music": "Apple Music", "spicy_lyrics": "Spicy Lyrics", "spotify": "Spotify"}
KEY_HELP = ("Spicy Lyrics rechazó tu llave. Usa una llave de cliente (empieza con sl_pk_) y activa «Allow requests with no Origin header» "
            "en developers.spicylyrics.org.")


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
        self.http = http or httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT})
        self.cache_dir = cache_dir if cache_dir is not None else app_data() / "cache" / "spicy"
        self.clock, self.sleep = clock, sleep
        self._lock, self._last, self._blocked_until = threading.Lock(), 0.0, 0.0

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
            raise SpicyKeyError("Falta la llave de Spicy Lyrics.")
        self._get(PROBE_ID, key)  # 200 y 404 significan que la llave pasó; el resto levanta

    # ---- red y caché ----
    def _get(self, track_id: str, key: str) -> httpx.Response:
        with self._lock:  # un hilo a la vez y con pausa mínima; si el servidor dijo «espera», ni se intenta
            now = self.clock()
            if now < self._blocked_until:
                raise ProviderError(f"Spicy Lyrics pidió esperar. Intenta de nuevo en {math.ceil(self._blocked_until - now)} s.")
            self.sleep(max(0.0, self._last + MIN_INTERVAL - now))
            try:
                r = self.http.get(f"{BASE}/lyrics/{track_id}", headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError as e:
                raise ProviderError("Spicy Lyrics no respondió. Revisa tu conexión y vuelve a intentar.") from e
            finally:
                self._last = self.clock()
            self._note_limits(r)
        if r.status_code == 429:
            raise ProviderError(f"Spicy Lyrics pide esperar {self._wait(r):.0f} s antes de volver a consultar. Intenta de nuevo en un momento.")
        if r.status_code in (401, 403):
            raise SpicyKeyError(KEY_HELP)
        if r.status_code >= 500:
            raise ProviderError("Spicy Lyrics está saturado o caído en este momento. Intenta de nuevo en unos minutos.")
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
            self._blocked_until = self.clock() + self._wait(r)

    def _cache_path(self, track_id: str) -> Path:
        return self.cache_dir / f"{track_id}.json"

    def _lookup(self, track_id: str, key: str) -> dict | None:
        path = self._cache_path(track_id)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - cached["t"] < (FOUND_TTL if cached["data"] else MISS_TTL):
                return cached["data"]
        except (OSError, ValueError, KeyError):
            pass
        r = self._get(track_id, key)
        try:
            raw = r.json() if r.status_code == 200 else None
        except ValueError:
            raw = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"t": time.time(), "data": raw}), encoding="utf-8")
        except OSError:
            pass  # sin caché también funciona
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
