"""Spicy Lyrics: mapeo de la respuesta (estructura verificada contra la API real, letra inventada), red, caché, límites y cadena de proveedores."""
import json

import httpx
import pytest

from marea.model.lyrics import Line, Lyrics, Syllable, spread
from marea.providers.errors import ProviderError
from marea.providers.lyrics import spicy
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.chain import ChainProvider
from marea.providers.lyrics.spicy import SpicyKeyError, SpicyLyricsProvider

TID = "7KNmIjcmGJIBrhP2s5Vioe"
KEY = "sl_pk_test"


def syl(text, b, e, part=False):
    return {"Text": text, "IsPartOfWord": part, "StartTime": b, "EndTime": e}


def vocal(syllables, start, end, opposite=False, background=None):
    item = {"Type": "Vocal", "OppositeAligned": opposite, "Lead": {"Syllables": syllables, "StartTime": start, "EndTime": end}}
    if background:
        item["Background"] = background
    return item


def body(content, type_="Syllable", source="apple_music", **extra):
    return {"Body": {"Type": type_, "SongWriters": ["A"], "StartTime": 1.0, "EndTime": 30.0, "id": TID, "source": source,
                     "Content": content, **extra}, "Status": 200, "Type": "object"}


SYLLABLE_BODY = body([
    vocal([syl("Hel", 1.0, 1.3, True), syl("lo", 1.3, 1.8, True), syl("world", 1.9, 2.5)], 1.0, 2.5),
    vocal([syl("Why", 4.0, 4.4), syl("not", 4.4, 5.0)], 3.9, 7.5, opposite=True,
          background=[{"Syllables": [syl("oh", 5.5, 6.0), syl("yeah", 6.0, 7.5)], "StartTime": 5.5, "EndTime": 7.5}]),
])


def provider(handler, tmp_path, key=KEY, now=None, **kw):
    clock = (lambda: now[0]) if now is not None else (lambda: 1000.0)
    return SpicyLyricsProvider(lambda: key, httpx.Client(transport=httpx.MockTransport(handler)), tmp_path, clock=clock,
                               sleep=lambda s: None, **kw)


def ok(payload):
    return lambda req: httpx.Response(200, json=payload)


Q = TrackQuery("t", "a", spotify_id=TID)


# ---------------- mapeo ----------------

def test_syllable_response_maps_words_agents_background_and_credit(tmp_path):
    ly = provider(ok(SYLLABLE_BODY), tmp_path).fetch(Q)
    assert ly.kind == "syllable" and ly.source == "apple_music" and ly.credit == "Apple Music" and ly.writers == ["A"]
    assert ly.credit_lines() == [("Written by", "A"), ("Provided by", "Apple Music")]  # el mismo texto que Spicetify
    first, second = ly.lines
    assert [(s.text, s.begin, s.end, s.word_end) for s in first.syllables] == [
        ("Hel", 1000, 1300, False), ("lo", 1300, 1800, False), ("world", 1900, 2500, False)]
    assert first.text == "Helloworld"  # IsPartOfWord=true pega con la siguiente: las tres son una sola palabra
    assert (first.start, first.end, first.agent) == (1000, 2500, "v1")
    assert second.agent == "v2"
    assert second.start == 3900 and second.end == 7500  # Lead.StartTime/EndTime mandan; el fin ya incluye el fondo
    (bg,) = second.bg
    assert [(s.text, s.begin) for s in bg.syllables] == [("oh", 5500), ("yeah", 6000)] and (bg.start, bg.end) == (5500, 7500)
    assert [s.word_end for s in second.syllables] == [True, False]  # «Why not»


def test_is_part_of_word_true_glues_to_the_next_syllable_not_the_previous():
    got = SpicyLyricsProvider._to_lyrics(body([vocal([syl("Mum-", 1, 2, True), syl("mum-", 2, 3, True), syl("mah", 3, 4), syl("yo", 4, 5)], 1, 5)]))
    assert got.lines[0].text == "Mum-mum-mah yo"


def test_community_credit_names_uploader_and_maker_and_is_sanitized():
    att = {"Uploader": {"username": "spikerko"}, "Maker": {"username": "gc\x00\x1b[31m‮" + "x" * 300}}
    ly = SpicyLyricsProvider._to_lyrics(body(SYLLABLE_BODY["Body"]["Content"], source="spicy_lyrics", UploadAttribution=att))
    assert ly.credit.startswith("Spicy Lyrics · uploaded by spikerko, made by gc[31mx") and ly.credit.endswith("…")
    assert len(ly.credit) < 100  # un maker de 300 caracteres no se come el crédito
    assert not any(c in ly.credit for c in "\x00\x1b‮")  # control y «right-to-left override» fuera
    anon = SpicyLyricsProvider._to_lyrics(body(SYLLABLE_BODY["Body"]["Content"], source="spicy_lyrics"))
    assert anon.credit == "Spicy Lyrics"
    assert SpicyLyricsProvider._to_lyrics(body(SYLLABLE_BODY["Body"]["Content"], source="spotify")).credit == "Spotify"


def test_writers_are_listed_sanitized_and_optional():
    many = body(SYLLABLE_BODY["Body"]["Content"], SongWriters=[f"Autor {i}" for i in range(30)] + ["x" * 99, "", None])
    ly = SpicyLyricsProvider._to_lyrics(many)
    assert ly.writers[:2] == ["Autor 0", "Autor 1"] and len(ly.writers) == 12  # tope: la lista de un sello no ocupa la pantalla
    nothing = SpicyLyricsProvider._to_lyrics(body(SYLLABLE_BODY["Body"]["Content"], SongWriters="no es lista"))
    assert nothing.writers == [] and nothing.credit_lines() == [("Provided by", "Apple Music")]


def test_line_type_is_spread_by_letters_and_flagged_approx():
    content = [{"Type": "Vocal", "OppositeAligned": False, "Text": "ab cdef", "StartTime": 10.0, "EndTime": 13.0},
               {"Type": "Vocal", "OppositeAligned": True, "Text": "sin fin", "StartTime": 14.0},
               {"Type": "Vocal", "Text": "ultima", "StartTime": 20.0}]
    ly = SpicyLyricsProvider._to_lyrics(body(content, type_="Line"))
    a, b, c = ly.lines
    assert ly.kind == "line" and a.approx and [(s.begin, s.end) for s in a.syllables] == [(10000, 11000), (11000, 13000)]
    assert b.end == 20000 and b.agent == "v2" and c.end == 25000  # sin EndTime: hasta el siguiente verso, o 5 s si es el último


def test_static_empty_and_malformed_content_give_no_lyrics_or_skip_the_bad_verse():
    assert SpicyLyricsProvider._to_lyrics(body([{"Text": "x"}], type_="Static")) is None
    assert SpicyLyricsProvider._to_lyrics({"Body": {"Type": "Syllable", "Content": []}}) is None
    assert SpicyLyricsProvider._to_lyrics({"Status": 404}) is None and SpicyLyricsProvider._to_lyrics([]) is None
    mixed = body([{"Lead": {"Syllables": [{"Text": "roto"}]}}, "basura", vocal([syl("bien", 2.0, 3.0)], 2.0, 3.0)])
    assert [l.text for l in SpicyLyricsProvider._to_lyrics(mixed).lines] == ["bien"]


# ---------------- red y caché ----------------

def test_request_uses_bearer_key_and_the_track_id_and_nothing_else(tmp_path):
    seen = []
    p = provider(lambda req: seen.append(req) or httpx.Response(200, json=SYLLABLE_BODY), tmp_path)
    assert p.fetch(Q) is not None
    (req,) = seen
    assert req.method == "GET" and str(req.url) == f"https://api.spicylyrics.org/v1/lyrics/{TID}"
    assert req.headers["Authorization"] == f"Bearer {KEY}"


def test_no_key_or_bad_id_means_no_request(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=SYLLABLE_BODY)

    assert provider(handler, tmp_path, key="  ").fetch(Q) is None
    p = provider(handler, tmp_path)
    for bad in ("", "short", "../../etc/passwd" + "x" * 10, TID + "/x", TID[:-1] + "!"):
        assert p.fetch(TrackQuery("t", "a", spotify_id=bad)) is None
    assert p.fetch(None) is None and not seen
    assert not any(tmp_path.glob("*"))  # un id raro no llega a tocar el disco


def test_found_and_missing_are_cached_across_restarts(tmp_path):
    calls = []
    p = provider(lambda req: calls.append(1) or httpx.Response(200, json=SYLLABLE_BODY), tmp_path)
    p.fetch(Q)

    def boom(req):
        raise httpx.ConnectError("sin red")

    assert provider(boom, tmp_path).fetch(Q).kind == "syllable" and len(calls) == 1  # otra instancia: sale de disco
    other = TrackQuery("t", "a", spotify_id="1QV6tiMFM6fSOKOGLMHYYg")
    missing = provider(lambda req: calls.append(1) or httpx.Response(404, json={"Status": 404}), tmp_path)
    assert missing.fetch(other) is None and missing.fetch(other) is None and len(calls) == 2  # «no hay» también se recuerda


def test_key_rejected_gets_actionable_message(tmp_path):
    for status in (401, 403):
        with pytest.raises(SpicyKeyError, match="Allow requests with no Origin header"):
            provider(lambda req, s=status: httpx.Response(s), tmp_path / str(status)).fetch(Q)


def test_server_and_network_errors_are_plain_provider_errors(tmp_path):
    with pytest.raises(ProviderError, match="saturado"):
        provider(lambda req: httpx.Response(503), tmp_path).fetch(Q)

    def boom(req):
        raise httpx.ConnectError("sin red")

    with pytest.raises(ProviderError, match="no respondió"):
        provider(boom, tmp_path / "n").fetch(Q)


def test_rate_limit_429_and_exhausted_window_stop_further_requests_until_reset(tmp_path):
    calls, now = [], [1000.0]

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"})

    p = provider(handler, tmp_path, now=now)
    with pytest.raises(ProviderError, match="7 s"):
        p.fetch(Q)
    with pytest.raises(ProviderError, match="en 7 s"):
        p.fetch(Q)  # ni siquiera sale a la red mientras dure la espera
    assert len(calls) == 1
    now[0] += 7.5
    with pytest.raises(ProviderError):
        p.fetch(Q)
    assert len(calls) == 2  # pasada la espera vuelve a intentar

    last = provider(lambda req: calls.append(1) or httpx.Response(200, json=SYLLABLE_BODY, headers={"ratelimit-remaining": "0", "ratelimit-reset": "12"}),
                    tmp_path / "w", now=now)
    assert last.fetch(Q) is not None  # esta respuesta sí se usa…
    with pytest.raises(ProviderError, match="12 s"):
        last.fetch(TrackQuery("t", "a", spotify_id="1QV6tiMFM6fSOKOGLMHYYg"))  # …y la siguiente espera la ventana


def test_check_key_accepts_found_and_missing_and_rejects_bad_keys(tmp_path):
    provider(ok(SYLLABLE_BODY), tmp_path / "a").check_key()
    provider(lambda req: httpx.Response(404), tmp_path / "b").check_key()  # la llave pasó aunque la pista no tenga letra
    with pytest.raises(SpicyKeyError):
        provider(lambda req: httpx.Response(403), tmp_path / "c").check_key()
    with pytest.raises(SpicyKeyError, match="Falta"):
        provider(ok(SYLLABLE_BODY), tmp_path / "d", key="").check_key()


def test_key_is_read_on_every_request_so_settings_changes_apply_immediately(tmp_path):
    keys, seen = ["", "sl_pk_one"], []
    p = SpicyLyricsProvider(lambda: keys[0], httpx.Client(transport=httpx.MockTransport(
        lambda req: seen.append(req.headers.get("Authorization")) or httpx.Response(200, json=SYLLABLE_BODY))), tmp_path, sleep=lambda s: None)
    assert p.fetch(Q) is None and not seen
    keys[0] = keys[1]
    assert p.fetch(Q) is not None and seen == ["Bearer sl_pk_one"]


# ---------------- cadena ----------------

def lyrics_of(kind):
    line = Line(0, 1000, syllables=spread("hola", 0, 1000), approx=kind == "line")
    return Lyrics([line], source=kind)


class Stub:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def fetch(self, q=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_chain_prefers_syllables_and_skips_later_providers_once_it_has_them():
    a, b = Stub(lyrics_of("syllable")), Stub(lyrics_of("line"))
    assert ChainProvider(a, b).fetch(Q).source == "syllable" and b.calls == 0


def test_chain_keeps_looking_when_the_first_is_only_line_synced():
    a, b = Stub(lyrics_of("line")), Stub(lyrics_of("syllable"))
    assert ChainProvider(a, b).fetch(Q).kind == "syllable"
    assert ChainProvider(Stub(lyrics_of("line")), Stub(None)).fetch(Q).kind == "line"  # nadie mejor: se queda con la de línea
    assert ChainProvider(Stub(None), Stub(None)).fetch(Q) is None


def test_chain_survives_a_failing_provider_and_only_raises_when_nobody_answers():
    bad = Stub(error=SpicyKeyError("llave"))
    assert ChainProvider(bad, Stub(lyrics_of("line"))).fetch(Q).kind == "line"
    with pytest.raises(SpicyKeyError):
        ChainProvider(bad, Stub(None)).fetch(Q)
    with pytest.raises(ProviderError, match="otra"):
        ChainProvider(Stub(error=ProviderError("otra")), bad).fetch(Q)  # el primer error es el que se informa
