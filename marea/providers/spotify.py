"""Spotify Web API: OAuth PKCE (sin client secret), metadatos, portada y reproducción actual.

El token vive en el keyring de Windows; el Client ID no es secreto (PKCE) y se guarda en los ajustes.
Respeta los límites de la API: 429 se informa con el tiempo de espera; no se reintenta en bucle.
"""
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring

from marea.audio.align import PositionSample
from marea.providers.errors import ProviderError

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"  # exactamente lo registrado en el dashboard de Spotify
SCOPES = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
KEYRING_SERVICE, KEYRING_USER = "ZH3 Marea", "spotify-token"
SEARCH_LIMIT = 10
EXPIRY_MARGIN = 60  # s: renovar el token un minuto antes de que caduque

_PAGE = """<!doctype html><meta charset="utf-8"><title>ZH3 Marea</title>
<body style="font-family:Segoe UI,sans-serif;background:#0E0D1A;color:#F4F3FA;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h2>{title}</h2><p style="opacity:.7">{text}</p></div>"""


class SpotifyError(ProviderError):
    pass


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    cover_url: str | None  # la imagen más grande que da Spotify (640 px)
    uri: str = ""

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @classmethod
    def from_api(cls, item: dict) -> "Track":
        images = sorted(item.get("album", {}).get("images") or [], key=lambda i: i.get("width") or 0, reverse=True)
        return cls(item["id"], item["name"], [a["name"] for a in item.get("artists", [])],
                   item.get("album", {}).get("name", ""), item.get("duration_ms", 0),
                   images[0]["url"] if images else None, item.get("uri", ""))


@dataclass
class Playback:
    track: Track
    progress_ms: int
    is_playing: bool
    timestamp_ms: int  # cuándo midió Spotify `progress_ms` (epoch ms); sirve para alinear el audio


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge S256) según RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 caracteres
    return verifier, pkce_challenge(verifier)


class TokenStore:
    """Tokens en el keyring de Windows, nunca en texto plano ni en el repo."""

    def load(self) -> dict | None:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def save(self, token: dict):
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps(token))

    def clear(self):
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass


class _Server(HTTPServer):
    allow_reuse_address = False  # en Windows SO_REUSEADDR deja que dos procesos usen el mismo puerto

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # nadie más puede enlazar ni «robar» el puerto del callback
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _bind() -> HTTPServer:
    try:
        return _Server(("127.0.0.1", REDIRECT_PORT), _Callback)
    except OSError as e:
        raise SpotifyError(f"El puerto {REDIRECT_PORT} está ocupado por otro programa. Ciérralo e intenta de nuevo.") from e


class _Callback(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        self.server.result = {k: v[0] for k, v in parse_qs(url.query).items()}
        ok = "code" in self.server.result
        body = _PAGE.format(title="Listo" if ok else "No se pudo conectar",
                            text="Ya puedes volver a ZH3 Marea." if ok else "Vuelve a ZH3 Marea e inténtalo de nuevo.")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # nada de logs: la URL lleva el código de autorización
        pass


def _serve(server: HTTPServer, timeout: float) -> dict | None:
    server.result, server.timeout = None, 0.5
    deadline = time.monotonic() + timeout
    while server.result is None and time.monotonic() < deadline:
        server.handle_request()
    return server.result


class SpotifyClient:
    def __init__(self, client_id: str, store: TokenStore | None = None, http: httpx.Client | None = None, now=time.time):
        self.client_id = client_id.strip()
        self.store = store or TokenStore()
        self.http = http or httpx.Client(timeout=10)
        self.now = now
        self._token = self.store.load()
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    # ---- autorización ----
    def authorize(self, open_browser=webbrowser.open, timeout: float = 180):
        """Abre el navegador, espera el redirect a 127.0.0.1 y canjea el código. Bloquea: usar en un hilo."""
        if not self.client_id:
            raise SpotifyError("Falta el Client ID de Spotify. Pégalo en Ajustes y vuelve a conectar.")
        verifier, challenge = make_pkce()
        state = secrets.token_urlsafe(16)
        server = _bind()  # antes de abrir el navegador, para no perder la respuesta
        try:
            open_browser(AUTH_URL + "?" + urlencode({
                "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPES,
                "code_challenge_method": "S256", "code_challenge": challenge, "state": state}))
            result = _serve(server, timeout)
        finally:
            server.server_close()
        if not result:
            raise SpotifyError("No recibí la autorización de Spotify a tiempo. Vuelve a intentar y acepta los permisos.")
        if result.get("state") != state:
            raise SpotifyError("La respuesta de Spotify no corresponde a esta solicitud. Vuelve a intentar.")
        if "error" in result:
            raise SpotifyError("Spotify no autorizó la conexión. Vuelve a intentar y acepta los permisos.")
        self._grant({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT_URI,
                     "code_verifier": verifier})

    def disconnect(self):
        with self._lock:
            self._token = None
        self.store.clear()

    def _grant(self, form: dict):
        try:
            r = self.http.post(TOKEN_URL, data={**form, "client_id": self.client_id})
        except httpx.HTTPError as e:
            raise SpotifyError("Spotify no respondió. Revisa tu conexión y vuelve a intentar.") from e
        if r.status_code != 200:
            err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error", "")
            if err == "invalid_client":
                raise SpotifyError("Spotify no reconoce ese Client ID. Revísalo en Ajustes (Dashboard de Spotify > tu app > Settings).")
            raise SpotifyError("Spotify rechazó la conexión. Vuelve a conectar la cuenta desde Ajustes.")
        data = r.json()
        with self._lock:
            old = self._token or {}
            self._token = {"access_token": data["access_token"],
                           "refresh_token": data.get("refresh_token", old.get("refresh_token")),
                           "expires_at": self.now() + data.get("expires_in", 3600)}
            self.store.save(self._token)

    # ---- peticiones ----
    def _access_token(self) -> str:
        if not self.connected:
            raise SpotifyError("Spotify no está conectado. Conéctalo en Ajustes.")
        if self.now() >= self._token["expires_at"] - EXPIRY_MARGIN:
            self._grant({"grant_type": "refresh_token", "refresh_token": self._token["refresh_token"]})
        return self._token["access_token"]

    def _request(self, method: str, url: str, allow: tuple = (), **kw) -> httpx.Response:
        """`allow`: códigos que el llamador interpreta él mismo (en los comandos de reproducción, 403 y 404 significan cosas distintas)."""
        for attempt in (1, 2):
            try:
                r = self.http.request(method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, **kw)
            except httpx.HTTPError as e:
                raise SpotifyError("Spotify no respondió. Revisa tu conexión y vuelve a intentar.") from e
            if r.status_code == 401 and attempt == 1:  # token revocado o caducado antes de lo esperado: renovar una vez
                self._grant({"grant_type": "refresh_token", "refresh_token": self._token["refresh_token"]})
                continue
            if r.status_code == 429:
                wait = r.headers.get("Retry-After", "unos")
                raise SpotifyError(f"Spotify pide esperar {wait} segundos antes de volver a consultar. Intenta de nuevo en un momento.")
            if r.status_code in allow:
                return r
            if r.status_code in (401, 403):
                raise SpotifyError("Spotify rechazó el acceso. Vuelve a conectar la cuenta desde Ajustes.")
            if r.status_code >= 400 and r.status_code != 404:
                raise SpotifyError("Spotify devolvió un error. Intenta de nuevo en unos minutos.")
            return r
        raise SpotifyError("Spotify rechazó el acceso. Vuelve a conectar la cuenta desde Ajustes.")

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[Track]:
        if not query.strip():
            return []
        r = self._request("GET", f"{API}/search", params={"q": query, "type": "track", "limit": limit})
        return [Track.from_api(i) for i in r.json().get("tracks", {}).get("items", []) if i]

    def now_playing(self) -> Playback | None:
        """Lo que suena ahora, o None si no hay nada (o es un podcast)."""
        r = self._request("GET", f"{API}/me/player/currently-playing")
        if r.status_code in (204, 404) or not r.content:
            return None
        d = r.json()
        item = d.get("item")
        if not item or item.get("type") != "track":
            return None
        return Playback(Track.from_api(item), d.get("progress_ms", 0), bool(d.get("is_playing")), d.get("timestamp", 0))

    def position_sample(self) -> tuple[Playback | None, PositionSample | None]:
        """Lo que suena y una medición de posición con los instantes de envío/recepción (alinea el audio capturado)."""
        t_sent = time.perf_counter()
        pb = self.now_playing()
        t_recv = time.perf_counter()
        return pb, (PositionSample(pb.progress_ms, t_sent, t_recv) if pb and pb.is_playing else None)

    # ---- control de reproducción (requiere Spotify Premium) ----
    def play(self, uri: str | None = None, position_ms: float = 0):
        """Reproduce `uri` desde `position_ms`; sin `uri` reanuda lo que estaba en pausa."""
        self._control("PUT", "play", {"uris": [uri], "position_ms": round(position_ms)} if uri else None)

    def pause(self):
        self._control("PUT", "pause", tolerate=True)  # ya estaba en pausa: Spotify responde 403 y el resultado es el pedido

    def next_track(self):
        self._control("POST", "next")

    def previous_track(self):
        self._control("POST", "previous")

    def _control(self, method: str, command: str, body: dict | None = None, tolerate: bool = False, device: str | None = None):
        r = self._request(method, f"{API}/me/player/{command}", allow=(403, 404),
                          params={"device_id": device} if device else None, json=body)
        if r.status_code == 404 and not device:  # Spotify abierto pero inactivo: se activa el dispositivo de este equipo
            if device := self._device_id():
                return self._control(method, command, body, tolerate, device)
        if r.status_code == 404:
            raise SpotifyError("Spotify no tiene un dispositivo activo. Abre Spotify en este equipo, dale play a cualquier canción y vuelve a intentar.")
        if r.status_code == 403:
            if "PREMIUM_REQUIRED" in r.text:
                raise SpotifyError("Controlar la reproducción requiere Spotify Premium.")
            if not tolerate:
                raise SpotifyError("Spotify no permitió ese comando en este momento. Intenta de nuevo.")

    def _device_id(self) -> str | None:
        """Dispositivo al que mandar los comandos cuando ninguno está activo: prefiere el activo y luego un equipo de escritorio."""
        r = self._request("GET", f"{API}/me/player/devices")
        devices = [d for d in (r.json().get("devices") or []) if d.get("id")] if r.status_code == 200 else []
        devices.sort(key=lambda d: (not d.get("is_active"), d.get("type") != "Computer"))
        return devices[0]["id"] if devices else None

    def cover(self, url: str) -> bytes:
        try:
            r = self.http.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SpotifyError("No pude descargar la portada. Revisa tu conexión y vuelve a intentar.") from e
        return r.content
