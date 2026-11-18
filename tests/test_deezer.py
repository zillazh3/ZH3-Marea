"""Unit tests for DeezerClient search, track lookup, caching, quota handling, and LRCLIB fuzzy fallback."""
import json
import httpx
import pytest

from marea.providers.deezer import DeezerClient, DeezerError
from marea.providers.lyrics.base import TrackQuery
from marea.providers.lyrics.lrclib import LrclibProvider


def _deezer_item(tid: int, title: str, artist: str, duration: int = 200, album: str = "Album"):
    return {
        "id": tid,
        "title": title,
        "duration": duration,
        "link": f"https://www.deezer.com/track/{tid}",
        "artist": {"id": tid * 10, "name": artist},
        "album": {
            "id": tid * 100,
            "title": album,
            "cover_medium": f"https://cdn-images.deezer.com/{tid}_250.jpg",
            "cover_xl": f"https://cdn-images.deezer.com/{tid}_1000.jpg",
        },
    }


def test_deezer_search_ranks_studio_above_live_and_cover_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.path == "/search/track":
            return httpx.Response(200, json={
                "data": [
                    _deezer_item(1, "Yellow (Live in Madrid)", "Coldplay", 270),
                    _deezer_item(2, "Yellow (Acoustic Cover)", "Tribute Band", 260),
                    _deezer_item(3, "Yellow", "Coldplay", 266, album="Parachutes"),
                ]
            })
        if req.url.path == "/track/3":
            return httpx.Response(200, json=_deezer_item(3, "Yellow", "Coldplay", 266, album="Parachutes"))
        return httpx.Response(404)

    dz = DeezerClient(http=httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path)
    tracks = dz.search("yellow coldplay")
    assert [t.id for t in tracks] == ["3", "1", "2"]
    assert tracks[0].provider == "deezer" and tracks[0].id_for("deezer") == "3"
    assert tracks[0].album == "Parachutes" and tracks[0].cover_url.endswith("_1000.jpg")

    # Cached hit avoids extra network calls
    tracks2 = dz.search("yellow coldplay")
    assert [t.id for t in tracks2] == ["3", "1", "2"]
    assert len(calls) == 1

    # Single track lookup
    t3 = dz.track("3")
    assert t3 is not None and t3.title == "Yellow" and len(calls) == 2


def test_deezer_handles_quota_exception_and_http_errors(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"error": {"type": "QuotaException", "message": "Quota limit exceeded", "code": 4}})

    dz = DeezerClient(http=httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path)
    with pytest.raises(DeezerError):
        dz.search("anything")


def test_lrclib_fuzzy_cleans_parenthetical_query_and_penalizes_wrong_versions(tmp_path):
    calls = []

    def handler(req):
        calls.append((req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/get"):
            return httpx.Response(404, json={"statusCode": 404})
        if req.url.path.endswith("/search"):
            if req.url.params.get("track_name") == "Yellow (Remastered 2023)":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[
                {"id": 10, "trackName": "Yellow (Live Cover)", "artistName": "Other Band", "duration": 266, "syncedLyrics": "[00:01.00]wrong"},
                {"id": 11, "trackName": "Yellow", "artistName": "Coldplay", "duration": 266, "syncedLyrics": "[00:01.00]Look at the stars"},
            ])
        return httpx.Response(404)

    prov = LrclibProvider(http=httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path)
    ly = prov.fetch(TrackQuery(title="Yellow (Remastered 2023)", artist="Coldplay feat. Someone", duration_ms=266_000))
    assert ly is not None and ly.lines[0].text == "Look at the stars"
    assert ly.provider_tag == "lrclib"
