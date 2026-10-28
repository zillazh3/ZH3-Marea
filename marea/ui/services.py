"""Servicios de la app (Spotify, letras) con estado y sondeo; la UI solo escucha señales."""
import skia
from PySide6.QtCore import QObject, QSettings, QTimer, Signal

from marea.demo_cover import demo_cover
from marea.project import ClipProject
from marea.secrets import KeyringSecrets
from marea.providers.errors import ProviderError
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.chain import ChainProvider
from marea.providers.lyrics.lrclib import LrclibProvider
from marea.providers.lyrics.spicy import SpicyLyricsProvider
from marea.providers.spotify import Playback, SpotifyClient, Track
from marea.ui.net import run_async

POLL_MS = 5000  # una consulta de «ahora suena» cada 5 s: lejos de los límites de Spotify
CONTROL_REFRESH_MS = 600  # tras play/pausa/siguiente: espera a que Spotify refleje el cambio antes de consultar


def decode_cover(data: bytes) -> skia.Image | None:
    return skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))


class Services(QObject):
    connection_changed = Signal(bool)
    now_playing = Signal(object)  # Playback | None
    problem = Signal(str)  # primer fallo de una racha (no repetir el mismo aviso cada 5 s)

    def __init__(self, settings: QSettings, spotify: SpotifyClient | None = None, lyrics=None, poll_ms: int = POLL_MS,
                 secrets=None, spicy=None):
        super().__init__()
        self.settings = settings
        self.secrets = secrets or KeyringSecrets()
        self.spicy = spicy or SpicyLyricsProvider(lambda: self.secrets.get("spicy_lyrics"))  # sin llave no consulta: se usa solo LRCLIB
        self.lyrics = lyrics or ChainProvider(self.spicy, LrclibProvider())
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

    def _make_client(self):
        self.spotify = SpotifyClient(self.client_id) if self.client_id else None

    def set_client_id(self, client_id: str):
        client_id = client_id.strip()
        if client_id == self.client_id:
            return
        if self.spotify:
            self.spotify.disconnect()  # el token pertenece al Client ID anterior
        self.settings.setValue("spotify/client_id", client_id)
        self._timer.stop()
        self._make_client()
        self.connection_changed.emit(False)

    def connect_spotify(self, ok=None, err=None):
        """Abre el navegador y espera la autorización (hasta 3 min) sin bloquear la UI."""
        if not self.spotify:
            if err:
                err(ProviderError("Falta el Client ID de Spotify. Pégalo en Ajustes y vuelve a conectar."))
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
                self.problem.emit(str(e) if isinstance(e, ProviderError) else "No pude consultar Spotify. Intenta de nuevo.")

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
            self.problem.emit(str(e) if isinstance(e, ProviderError) else "No pude controlar Spotify. Intenta de nuevo.")

        run_async(run, ok, err)

    # ---- bloqueantes: llamar con run_async ----
    def cover_bytes(self, url: str) -> bytes:
        if url not in self._covers:
            self._covers[url] = self.spotify.cover(url)
        return self._covers[url]

    def _lyrics_for(self, t: Track):
        return self.lyrics.fetch(TrackQuery(t.title, t.artists[0] if t.artists else "", t.album, t.duration_ms, t.id))

    def lyrics_kind(self, t: Track) -> str:
        """'syllable' | 'line' | 'none' | 'error'."""
        try:
            ly = self._lyrics_for(t)
        except ProviderError:
            return "error"
        return ly.kind if ly else "none"

    def build_project(self, t: Track) -> ClipProject:
        ly = self._lyrics_for(t)
        if not ly:
            raise ProviderError("No encontré una letra sincronizada para esta canción. Prueba con otra versión o abre un .ttml propio.")
        cover = None
        if t.cover_url:
            try:
                cover = decode_cover(self.cover_bytes(t.cover_url))
            except ProviderError:
                pass  # sin portada se puede seguir: se usa una de relleno
        return ClipProject(ly, t.title, t.artist, cover or demo_cover(label=False), track=t)
