import json
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend

from marea.model.lyrics import parse_lyricsfile
from marea.providers import spotify as sp
from marea.providers.errors import ProviderError
from marea.providers.lyrics import lrclib
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.lrclib import LrclibProvider
from marea.providers.spotify import SpotifyClient, SpotifyError, Track

CLIENT_ID = "abc123clientid"


class MemStore:
    def __init__(self, token=None):
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = dict(token)

    def clear(self):
        self.token = None


def client(handler, token=None, now=lambda: 1000.0):
    return SpotifyClient(CLIENT_ID, MemStore(token), httpx.Client(transport=httpx.MockTransport(handler)), now)


GOOD = {"access_token": "AT", "refresh_token": "RT", "expires_at": 5000.0}


def track_json(**kw):
    return {"id": "t1", "name": "Canción", "uri": "spotify:track:t1", "duration_ms": 200000,
            "artists": [{"name": "A"}, {"name": "B"}], "type": "track",
            "album": {"name": "Disco", "images": [{"url": "small", "width": 64}, {"url": "big", "width": 640},
                                                  {"url": "mid", "width": 300}]}, **kw}


# ---------------- PKCE y autorización ----------------

def test_pkce_matches_rfc7636_vector_and_is_random():
    assert sp.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    (v1, c1), (v2, _) = sp.make_pkce(), sp.make_pkce()
    assert v1 != v2 and 43 <= len(v1) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", v1) and c1 == sp.pkce_challenge(v1)


def test_redirect_uri_is_the_registered_loopback():
    assert sp.REDIRECT_URI == "http://127.0.0.1:8765/callback"


def authorize_with(c, mutate=lambda q: q, timeout=10):
    """Simula al usuario: el «navegador» abre la URL y redirige al callback local (con `mutate` aplicado a la query)."""
    seen = {}

    def browser(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        seen.update(q, url=url)
        cb = mutate({"code": "THE_CODE", "state": q["state"]})
        threading.Thread(target=lambda: httpx.get(sp.REDIRECT_URI, params=cb, timeout=5), daemon=True).start()

    try:
        c.authorize(open_browser=browser, timeout=timeout)
    finally:
        authorize_with.seen = seen
    return seen


def test_authorize_full_flow_binds_verifier_to_challenge_and_stores_token():
    posted = []

    def handler(req):
        posted.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})

    c = client(handler)
    seen = authorize_with(c)
    assert urlparse(seen["url"]).netloc == "accounts.spotify.com"
    assert (seen["client_id"], seen["response_type"], seen["redirect_uri"], seen["code_challenge_method"]) == (
        CLIENT_ID, "code", "http://127.0.0.1:8765/callback", "S256")
    assert "user-read-currently-playing" in seen["scope"]
    (form,) = posted
    assert (form["grant_type"], form["code"], form["client_id"], form["redirect_uri"]) == (
        "authorization_code", "THE_CODE", CLIENT_ID, sp.REDIRECT_URI)
    assert "client_secret" not in form and sp.pkce_challenge(form["code_verifier"]) == seen["code_challenge"]
    assert c.connected and c.store.token == {"access_token": "AT1", "refresh_token": "RT1", "expires_at": 4600.0}


def test_authorize_rejects_wrong_state_without_exchanging():
    calls = []
    c = client(lambda req: calls.append(req) or httpx.Response(200, json={}))
    with pytest.raises(SpotifyError, match="no corresponde"):
        authorize_with(c, mutate=lambda q: {**q, "state": "otro"})
    assert not calls and not c.connected


def test_authorize_user_denied_and_timeout_and_busy_port_and_missing_id():
    c = client(lambda req: httpx.Response(200, json={}))
    with pytest.raises(SpotifyError, match="no autorizó"):
        authorize_with(c, mutate=lambda q: {"error": "access_denied", "state": q["state"]})
    with pytest.raises(SpotifyError, match="a tiempo"):
        c.authorize(open_browser=lambda url: None, timeout=0.6)
    busy = sp._Server(("127.0.0.1", sp.REDIRECT_PORT), sp._Callback)  # otro programa que ya usa el puerto
    try:
        with pytest.raises(SpotifyError, match="puerto 8765"):
            c.authorize(open_browser=lambda url: None, timeout=0.5)
    finally:
        busy.server_close()
    with pytest.raises(SpotifyError, match="Client ID"):
        SpotifyClient("  ", MemStore(), httpx.Client(transport=httpx.MockTransport(lambda r: None))).authorize()


def test_invalid_client_id_gets_a_fixable_message():
    c = client(lambda req: httpx.Response(400, json={"error": "invalid_client"}))
    with pytest.raises(SpotifyError, match="Client ID"):
        authorize_with(c)


# ---------------- token: refresh, 401, 429 ----------------

def test_expired_token_is_refreshed_keeping_the_refresh_token():
    clock, forms = [1000.0], []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            forms.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
            return httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})  # sin refresh_token nuevo
        assert req.headers["Authorization"] == "Bearer NEW"
        return httpx.Response(200, json={"tracks": {"items": []}})

    c = client(handler, dict(GOOD), now=lambda: clock[0])
    clock[0] = 4990.0  # a menos de un minuto de caducar
    c.search("x")
    assert forms == [{"grant_type": "refresh_token", "refresh_token": "RT", "client_id": CLIENT_ID}]
    assert c.store.token["refresh_token"] == "RT" and c.store.token["access_token"] == "NEW"


def test_401_refreshes_once_and_retries_then_asks_to_reconnect():
    seq = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            seq.append("refresh")
            return httpx.Response(200, json={"access_token": "N2", "expires_in": 3600})
        seq.append("api")
        return httpx.Response(401) if len([s for s in seq if s == "api"]) == 1 else httpx.Response(200, json={"tracks": {"items": []}})

    assert client(handler, dict(GOOD)).search("x") == [] and seq == ["api", "refresh", "api"]

    def always_401(req):
        return httpx.Response(200, json={"access_token": "N", "expires_in": 3600}) if req.url.host == "accounts.spotify.com" else httpx.Response(401)

    with pytest.raises(SpotifyError, match="Vuelve a conectar"):
        client(always_401, dict(GOOD)).search("x")


def test_429_reports_wait_time_and_does_not_retry():
    calls = []
    c = client(lambda req: calls.append(1) or httpx.Response(429, headers={"Retry-After": "7"}), dict(GOOD))
    with pytest.raises(SpotifyError, match="7 segundos"):
        c.search("x")
    assert len(calls) == 1


def test_network_failure_and_not_connected_messages():
    def boom(req):
        raise httpx.ConnectError("sin red")

    with pytest.raises(SpotifyError, match="Revisa tu conexión"):
        client(boom, dict(GOOD)).search("x")
    with pytest.raises(SpotifyError, match="no está conectado"):
        client(boom).search("x")
    c = client(boom, dict(GOOD))
    c.disconnect()
    assert not c.connected and c.store.token is None


# ---------------- datos ----------------

def test_search_parses_tracks_and_picks_biggest_cover():
    seen = []
    c = client(lambda req: seen.append(req) or httpx.Response(200, json={"tracks": {"items": [track_json(), None]}}), dict(GOOD))
    (t,) = c.search("hola mundo")
    assert (t.id, t.title, t.artist, t.album, t.duration_ms, t.cover_url) == ("t1", "Canción", "A, B", "Disco", 200000, "big")
    assert dict(seen[0].url.params) == {"q": "hola mundo", "type": "track", "limit": "10"}
    assert c.search("   ") == [] and len(seen) == 1  # consulta vacía: ni siquiera se llama a la API


def test_now_playing_variants():
    def respond(status=200, body=None):
        return client(lambda req: httpx.Response(status, json=body) if body is not None else httpx.Response(status), dict(GOOD))

    assert respond(204).now_playing() is None
    assert respond(200, {"item": {"type": "episode", "id": "e"}, "is_playing": True}).now_playing() is None
    p = respond(200, {"item": track_json(), "progress_ms": 1234, "is_playing": True, "timestamp": 1700000000000}).now_playing()
    assert (p.track.title, p.progress_ms, p.is_playing, p.timestamp_ms) == ("Canción", 1234, True, 1700000000000)


# ---------------- control de reproducción ----------------

def test_client_implements_what_the_recording_session_calls():
    """record_song_range llama estos tres métodos; durante meses solo existían en el cliente falso de los tests."""
    from marea.audio import session
    import inspect
    used = set(re.findall(r"client\.(\w+)\(", inspect.getsource(session.record_song_range)))
    assert used == {"play", "pause", "position_sample"}
    assert all(callable(getattr(SpotifyClient, name)) for name in used)


def test_play_sends_uri_and_rounded_position_and_pause_next_previous_use_the_right_verbs():
    seen = []
    c = client(lambda req: seen.append(req) or httpx.Response(204), dict(GOOD))
    c.play("spotify:track:t1", 59_750.4)
    c.play()  # sin pista: reanudar
    c.pause()
    c.next_track()
    c.previous_track()
    assert [(r.method, r.url.path) for r in seen] == [("PUT", "/v1/me/player/play"), ("PUT", "/v1/me/player/play"),
                                                      ("PUT", "/v1/me/player/pause"), ("POST", "/v1/me/player/next"),
                                                      ("POST", "/v1/me/player/previous")]
    assert json.loads(seen[0].content) == {"uris": ["spotify:track:t1"], "position_ms": 59750}
    assert seen[1].content == b"" and seen[0].headers["Authorization"] == "Bearer AT"


def test_play_without_active_device_targets_the_computer_and_retries_once():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.url.params.get("device_id")))
        if req.url.path.endswith("/devices"):
            return httpx.Response(200, json={"devices": [{"id": "phone", "type": "Smartphone", "is_active": False},
                                                         {"id": None, "type": "Computer"},
                                                         {"id": "pc", "type": "Computer", "is_active": False}]})
        return httpx.Response(404, json={"error": {"reason": "NO_ACTIVE_DEVICE"}}) if not req.url.params.get("device_id") else httpx.Response(204)

    client(handler, dict(GOOD)).play("spotify:track:t1", 1000)
    assert seen == [("PUT", "/v1/me/player/play", None), ("GET", "/v1/me/player/devices", None), ("PUT", "/v1/me/player/play", "pc")]


def test_play_without_any_device_explains_how_to_fix_it():
    def handler(req):
        return httpx.Response(200, json={"devices": []}) if req.url.path.endswith("/devices") else httpx.Response(404)

    with pytest.raises(SpotifyError, match="dispositivo activo"):
        client(handler, dict(GOOD)).play("spotify:track:t1")


def test_premium_required_is_explained_and_other_403_is_not_a_reconnect_message():
    premium = client(lambda req: httpx.Response(403, json={"error": {"reason": "PREMIUM_REQUIRED"}}), dict(GOOD))
    for call in (lambda: premium.play("u"), premium.pause, premium.next_track):
        with pytest.raises(SpotifyError, match="Premium"):
            call()
    restricted = client(lambda req: httpx.Response(403, json={"error": {"reason": "UNKNOWN"}}), dict(GOOD))
    restricted.pause()  # ya estaba en pausa: no es un error
    with pytest.raises(SpotifyError, match="no permitió"):
        restricted.next_track()


def test_search_still_treats_403_as_reconnect():
    with pytest.raises(SpotifyError, match="Vuelve a conectar"):
        client(lambda req: httpx.Response(403), dict(GOOD)).search("x")


def test_position_sample_brackets_the_request_with_the_local_clock():
    body = {"item": track_json(), "progress_ms": 4321, "is_playing": True, "timestamp": 1}
    c = client(lambda req: httpx.Response(200, json=body), dict(GOOD))
    before = time.perf_counter()
    pb, sample = c.position_sample()
    after = time.perf_counter()
    assert pb.track.id == "t1" and sample.progress_ms == 4321
    assert before <= sample.t_sent <= sample.t_recv <= after
    body["is_playing"] = False
    pb, sample = c.position_sample()
    assert pb is not None and sample is None  # en pausa la posición no avanza: no sirve para alinear
    assert client(lambda req: httpx.Response(204), dict(GOOD)).position_sample() == (None, None)


def test_cover_download_errors_are_explained():
    ok = client(lambda req: httpx.Response(200, content=b"PNGDATA"), dict(GOOD))
    assert ok.cover("https://i.scdn.co/x") == b"PNGDATA"
    with pytest.raises(SpotifyError, match="portada"):
        client(lambda req: httpx.Response(404), dict(GOOD)).cover("https://i.scdn.co/x")


def test_token_store_roundtrip_in_keyring(monkeypatch):
    class Mem(KeyringBackend):
        priority = 1
        d = {}

        def get_password(self, s, u):
            return self.d.get((s, u))

        def set_password(self, s, u, p):
            self.d[(s, u)] = p

        def delete_password(self, s, u):
            self.d.pop((s, u), None)

    monkeypatch.setattr(keyring, "get_keyring", lambda: Mem())
    keyring.set_keyring(Mem())
    store = sp.TokenStore()
    assert store.load() is None
    store.save(GOOD)
    assert store.load() == GOOD and json.loads(Mem.d[("ZH3 Marea", "spotify-token")]) == GOOD
    store.clear()
    store.clear()  # borrar dos veces no truena
    assert store.load() is None


# ---------------- LRCLIB ----------------

LRC = "[ar:x]\n[00:10.00]uno dos\n[00:14.00]tres cuatro\n[00:19.00]\n"
SPEC_YAML = """version: '1.0'
metadata: {title: t, artist: a}
lines:
  - text: 'Hello world'
    start_ms: 1200
    end_ms: 2800
    words:
      - {text: 'Hello ', start_ms: 1200, end_ms: 1900}
      - {text: 'world', start_ms: 1900, end_ms: 2800}
"""


def lrc_provider(handler, tmp_path):
    return LrclibProvider(httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": lrclib.USER_AGENT}),
                          tmp_path / "cache")


Q = TrackQuery("Canción", "Artista", "Disco", 200_000)


def test_lrclib_get_hit_sends_params_and_user_agent_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"syncedLyrics": LRC, "duration": 200, "hasWordSync": False})

    p = lrc_provider(handler, tmp_path)
    ly = p.fetch(Q)
    assert ly.kind == "line" and [l.text for l in ly.lines] == ["uno dos", "tres cuatro"] and ly.lines[1].end == 19000
    assert dict(calls[0].url.params) == {"track_name": "Canción", "artist_name": "Artista", "album_name": "Disco", "duration": "200"}
    assert calls[0].headers["User-Agent"] == "ZH3 Marea/0.1"
    assert p.fetch(Q) is not None and len(calls) == 1  # la segunda vez sale del disco
    assert LrclibProvider(httpx.Client(transport=httpx.MockTransport(lambda r: 1 / 0)), tmp_path / "cache").fetch(Q)  # y tras reiniciar


def test_lrclib_falls_back_to_search_and_filters_by_duration(tmp_path):
    def handler(req):
        if req.url.path.endswith("/get"):
            return httpx.Response(404, json={"statusCode": 404})
        return httpx.Response(200, json=[
            {"duration": 260, "syncedLyrics": LRC},  # otra versión (muy distinta)
            {"duration": 203, "syncedLyrics": LRC.replace("uno", "UNO")},
            {"duration": 199, "syncedLyrics": LRC.replace("uno", "MEJOR")},
            {"duration": 200, "syncedLyrics": None, "plainLyrics": "solo texto"}])

    assert lrc_provider(handler, tmp_path).fetch(Q).lines[0].text == "MEJOR dos"
    assert lrc_provider(lambda r: httpx.Response(404) if r.url.path.endswith("/get") else httpx.Response(200, json=[{"duration": 400, "syncedLyrics": LRC}]),
                        tmp_path / "otro").fetch(Q) is None


def test_lrclib_no_lyrics_cases_and_miss_is_cached(tmp_path):
    calls = []
    p = lrc_provider(lambda req: calls.append(1) or httpx.Response(200, json={"instrumental": True, "syncedLyrics": LRC}), tmp_path)
    assert p.fetch(Q) is None
    assert lrc_provider(lambda r: httpx.Response(200, json={"plainLyrics": "sin tiempos", "syncedLyrics": ""}), tmp_path / "b").fetch(Q) is None
    n = len(calls)
    miss = lrc_provider(lambda r: calls.append(1) or httpx.Response(404), tmp_path / "c")
    assert miss.fetch(Q) is None
    n2 = len(calls)
    assert miss.fetch(Q) is None and len(calls) == n2 > n  # el «no hay» también se recuerda
    assert p.fetch(TrackQuery(title="solo título")) is None and p.fetch(None) is None  # sin artista: ni se consulta


def test_lrclib_uses_word_sync_when_available_and_falls_back(tmp_path):
    ly = lrc_provider(lambda r: httpx.Response(200, json={"hasWordSync": True, "lyricsfile": SPEC_YAML, "syncedLyrics": LRC}),
                      tmp_path).fetch(Q)
    assert ly.kind == "syllable" and [(s.text, s.begin, s.end, s.word_end) for s in ly.lines[0].syllables] == [
        ("Hello", 1200, 1900, True), ("world", 1900, 2800, False)]
    broken = lrc_provider(lambda r: httpx.Response(200, json={"hasWordSync": True, "lyricsfile": "version: '9'\n", "syncedLyrics": LRC}),
                          tmp_path / "x").fetch(Q)
    assert broken.kind == "line" and len(broken.lines) == 2


def test_lrclib_errors_are_user_facing_and_throttled(tmp_path):
    with pytest.raises(ProviderError, match="no respondió"):
        lrc_provider(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")), tmp_path).fetch(Q)
    with pytest.raises(ProviderError, match="saturado"):
        lrc_provider(lambda r: httpx.Response(503), tmp_path / "b").fetch(Q)
    p = lrc_provider(lambda r: httpx.Response(404), tmp_path / "c")
    t0 = time.monotonic()
    p.fetch(Q)  # /get 404 + /search: dos peticiones seguidas
    assert time.monotonic() - t0 >= lrclib.MIN_INTERVAL * 0.9


# ---------------- lyricsfile ----------------

def test_parse_lyricsfile_variants():
    ly = parse_lyricsfile("""version: '1.0'
metadata: {title: t, artist: a}
lines:
  - {text: 'Segundo verso', start_ms: 5000}
  - {text: 'Primero', start_ms: 1000, end_ms: 4000}
  - {text: '   ', start_ms: 4500}
  - {text: 'Se traslapa', start_ms: 3000, end_ms: 6000}
""")
    assert [l.start for l in ly.lines] == [1000, 3000, 5000]  # ordenadas; la vacía se descarta; traslape permitido
    assert ly.lines[2].end == 5000 + 5000 and ly.lines[0].approx and ly.kind == "line"  # sin end_ms ni verso siguiente: tope de 5 s
    for bad in ("version: '2.0'\nlines: []", "- no\n- es\n- mapa", "a: [unclosed"):
        with pytest.raises(ValueError):
            parse_lyricsfile(bad)
    with pytest.raises(ValueError, match="grande"):
        parse_lyricsfile("x" * 1_000_001)
