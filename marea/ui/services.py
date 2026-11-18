"""Servicios de la app (Spotify, Deezer, enlaces, letras) con estado y sondeo; la UI solo escucha señales."""
from dataclasses import dataclass

import httpx
import skia
from PySide6.QtCore import QObject, QSettings, QTimer, Signal

from marea.async_util import run_async
from marea.demo_cover import demo_cover
from marea.model.lyrics import SOURCE_LRCLIB, SOURCE_SPICY, SOURCE_UNKNOWN, Lyrics
from marea.project import ClipProject
from marea.providers.deezer import DeezerClient
from marea.providers.errors import ProviderError
from marea.providers.links import parse_music_link, resolve_link_metadata
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.chain import ChainProvider, validate_lyrics
from marea.providers.lyrics.lrclib import LrclibProvider
from marea.providers.lyrics.spicy import SpicyLyricsProvider
from marea.providers.matching import RankedTrack, needs_disambiguation, rank_tracks
from marea.providers.spotify import Playback, SpotifyClient
from marea.providers.track import Track
from marea.secrets import KeyringSecrets
from marea.ui import lang

POLL_MS = 5000  # una consulta de «ahora suena» cada 5 s: lejos de los límites de Spotify
CONTROL_REFRESH_MS = 600  # tras play/pausa/siguiente: espera a que Spotify refleje el cambio antes de consultar


@dataclass(frozen=True)
class LyricsProbe:
    kind: str  # "syllable" | "line" | "none" | "error"
    source_tag: str  # "spicy_lyrics" | "lrclib" | "manual" | "unknown"
    credit: str = ""


def decode_cover(data: bytes) -> skia.Image | None:
    return skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))


class Services(QObject):
    connection_changed = Signal(bool)
    now_playing = Signal(object)  # Playback | None
    problem = Signal(str)  # primer fallo de una racha (no repetir el mismo aviso cada 5 s)

    def __init__(self, settings: QSettings, spotify: SpotifyClient | None = None, lyrics=None, poll_ms: int = POLL_MS,
                 secrets=None, spicy=None, deezer=None, lrclib=None):
        super().__init__()
        self.settings = settings
        self.secrets = secrets or KeyringSecrets()
        self.spicy = spicy or SpicyLyricsProvider(lambda: self.secrets.get("spicy_lyrics"))  # sin llave no consulta: se usa solo LRCLIB
        self.lrclib = lrclib or LrclibProvider()
        self.lyrics = lyrics or ChainProvider(self.spicy, self.lrclib)
        self.deezer = deezer or DeezerClient()
        self._cover_http = httpx.Client(timeout=10)
        self.spotify = spotify
        if spotify is None:
            self._make_client()
        self._covers: dict[str, bytes] = {}
        self._polling = self._failing = self._controlling = False
        self.playback: Playback | None = None  # lo último que reportó «ahora suena»
        self._timer = QTimer(self, interval=poll_ms)
        self._timer.timeout.connect(self.poll)
        if self.connected:
            self._timer.start()

    # ---- cuenta ----
    @property
    def client_id(self) -> str:
        return self.settings.value("spotify/client_id", "") or ""

    @property
    def connected(self) -> bool:
        return bool(self.spotify and self.spotify.connected)

    def close(self):
        self._timer.stop()
        for client in (self.spotify, self.deezer, self.spicy, self.lrclib, self._cover_http,
                       *getattr(self.lyrics, "providers", ())):
            fn = getattr(client, "close", None)
            if fn:
                fn()

    def _make_client(self):
        self.spotify = SpotifyClient(self.client_id) if self.client_id else None

    def set_client_id(self, client_id: str):
        client_id = client_id.strip()
        if client_id == self.client_id:
            return
        if self.spotify:
            self.spotify.disconnect()  # el token pertenece al Client ID anterior
            if close_fn := getattr(self.spotify, "close", None):
                close_fn()
        self.settings.setValue("spotify/client_id", client_id)
        self._timer.stop()
        self._make_client()
        self.connection_changed.emit(False)

    def connect_spotify(self, ok=None, err=None):
        """Abre el navegador y espera la autorización (hasta 3 min) sin bloquear la UI."""
        if not self.spotify:
            if err:
                err(ProviderError(lang.t("services.missingClientId")))
            return

        def done(_):
            self._timer.start()
            self.connection_changed.emit(True)
            self.poll()
            if ok:
                ok(None)

        run_async(self.spotify.authorize, done, err)

    def disconnect_spotify(self):
        if self.spotify:
            self.spotify.disconnect()
        self._timer.stop()
        self.playback = None
        self.connection_changed.emit(False)
        self.now_playing.emit(None)

    # ---- ahora suena ----
    def poll(self):
        if self._polling or not self.connected:
            return
        self._polling = True

        def ok(pb: Playback | None):
            self._polling = self._failing = False
            self.playback = pb
            self.now_playing.emit(pb)

        def err(e):
            self._polling = False
            if not self._failing:
                self._failing = True
                self.problem.emit(str(e) if isinstance(e, ProviderError) else lang.t("services.pollFailed"))

        run_async(self.spotify.now_playing, ok, err)

    def control(self, action: str):
        """'toggle' (play/pausa), 'next' o 'previous' sobre Spotify, en un hilo. Después refresca «ahora suena»."""
        if not self.connected or self._controlling:
            return
        self._controlling = True
        s = self.spotify
        run = {"toggle": s.pause if self.playback and self.playback.is_playing else s.play,
               "next": s.next_track, "previous": s.previous_track}[action]

        def ok(_):
            self._controlling = False
            QTimer.singleShot(CONTROL_REFRESH_MS, self.poll)  # Spotify tarda un instante en reflejar el cambio

        def err(e):
            self._controlling = False
            self.problem.emit(str(e) if isinstance(e, ProviderError) else lang.t("services.controlFailed"))

        run_async(run, ok, err)

    # ---- bloqueantes: llamar con run_async ----
    def cover_bytes(self, url: str) -> bytes:
        if url not in self._covers:
            if self.spotify is not None and hasattr(self.spotify, "cover") and ("scdn.co" in url or not url.startswith("http")):
                self._covers[url] = self.spotify.cover(url)
            else:
                try:
                    r = self._cover_http.get(url)
                    r.raise_for_status()
                    self._covers[url] = r.content
                except httpx.HTTPError as e:
                    raise ProviderError(lang.t("errors.spotify.coverDownload")) from e
        return self._covers[url]

    def search_tracks(self, query: str, *, prefer_deezer: bool = False) -> tuple[list[Track], str]:
        """Search Spotify when connected, falling back to Deezer or resolving pasted music links."""
        q = (query or "").strip()
        if not q:
            return [], ""
        if parsed := parse_music_link(q):
            res = resolve_link_metadata(
                parsed,
                spotify=None if prefer_deezer else self.spotify,
                deezer=self.deezer,
                http=self._cover_http,
            )
            prov = res.tracks[0].provider if res.tracks else parsed.service
            return res.tracks, prov

        spotify_err: ProviderError | None = None
        if self.connected and not prefer_deezer:
            try:
                tracks = self.spotify.search(q)
                if tracks:
                    return tracks, "spotify"
            except ProviderError as e:
                spotify_err = e

        try:
            dz_tracks = self.deezer.search(q)
        except ProviderError as e:
            raise (spotify_err or e)
        if not dz_tracks and spotify_err is not None:
            raise spotify_err
        return dz_tracks, "deezer"

    def resolve_metadata(
        self,
        query: str = "",
        *,
        title: str = "",
        artist: str = "",
        duration_ms: int | None = None,
    ) -> tuple[list[RankedTrack], bool]:
        """Search and rank metadata candidates, flagging ambiguous results for manual selection."""
        q = (query or "").strip() or " ".join(p for p in (artist, title) if p).strip()
        if not q:
            return [], False
        if parsed := parse_music_link(q):
            res = resolve_link_metadata(parsed, spotify=self.spotify, deezer=self.deezer, http=self._cover_http)
            tracks = res.tracks
            q_title = res.title or title
            q_artist = res.artist or artist
            q_dur = res.duration_ms or duration_ms
        else:
            tracks, _ = self.search_tracks(q)
            q_title, q_artist, q_dur = title, artist, duration_ms
        ranked = rank_tracks(
            tracks,
            query_text=q if not q_title else "",
            query_title=q_title,
            query_artist=q_artist,
            query_duration_ms=q_dur,
        )
        return ranked, needs_disambiguation(ranked)

    @staticmethod
    def track_query(t: Track) -> TrackQuery:
        return TrackQuery(
            t.title,
            t.artists[0] if t.artists else "",
            t.album,
            t.duration_ms,
            spotify_id=t.id_for("spotify"),
            deezer_id=t.id_for("deezer"),
        )

    def _lyrics_for(self, t: Track) -> Lyrics | None:
        return self.lyrics.fetch(self.track_query(t))

    def fetch_lyrics_for_track(self, t: Track, *, provider: str = "auto") -> Lyrics | None:
        """Fetch lyrics via full chain or an explicit provider ('spicy_lyrics' | 'lrclib')."""
        q = self.track_query(t)
        if provider == SOURCE_SPICY:
            ly = self.spicy.fetch(q)
            if validate_lyrics(ly):
                ly.provider_tag = SOURCE_SPICY
                return ly
            return None
        if provider == SOURCE_LRCLIB:
            ly = self.lrclib.fetch(q)
            if validate_lyrics(ly):
                ly.provider_tag = SOURCE_LRCLIB
                return ly
            return None
        return self.lyrics.fetch(q)

    def search_lyrics_candidates(self, title: str, artist: str, album: str = "", duration_ms: int | None = None):
        return self.lrclib.search_candidates(TrackQuery(title, artist, album, duration_ms))

    def lyrics_probe(self, t: Track) -> LyricsProbe:
        try:
            ly = self._lyrics_for(t)
        except ProviderError:
            return LyricsProbe("error", SOURCE_UNKNOWN)
        if not ly:
            return LyricsProbe("none", SOURCE_UNKNOWN)
        return LyricsProbe(ly.kind, ly.resolved_source_tag, ly.credit)

    def lyrics_kind(self, t: Track) -> str:
        """'syllable' | 'line' | 'none' | 'error'."""
        return self.lyrics_probe(t).kind

    def build_project(self, t: Track) -> ClipProject:
        ly = self._lyrics_for(t)
        if not ly:
            raise ProviderError(lang.t("services.noLyrics"))
        cover = None
        if t.cover_url:
            try:
                cover = decode_cover(self.cover_bytes(t.cover_url))
            except ProviderError:
                pass  # sin portada se puede seguir: se usa una de relleno
        return ClipProject(ly, t.title, t.artist, cover or demo_cover(label=False), track=t, album=t.album)
