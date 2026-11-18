"""Unit tests for music link parsing and cross-provider link resolution."""
import httpx

from marea.providers.deezer import DeezerClient
from marea.providers.links import parse_music_link, resolve_link_metadata


def test_parse_music_links_across_platforms():
    sp = parse_music_link("https://open.spotify.com/intl-es/track/7KNmIjcmGJIBrhP2s5Vioe?si=abc")
    assert sp and sp.service == "spotify" and sp.kind == "track" and sp.track_id == "7KNmIjcmGJIBrhP2s5Vioe"

    sp_uri = parse_music_link("spotify:track:7KNmIjcmGJIBrhP2s5Vioe")
    assert sp_uri and sp_uri.service == "spotify" and sp_uri.track_id == "7KNmIjcmGJIBrhP2s5Vioe"

    am = parse_music_link("https://music.apple.com/mx/album/parachutes/1122782080?i=1122782283")
    assert am and am.service == "apple_music" and am.kind == "track"
    assert am.track_id == "1122782283" and am.album_id == "1122782080" and am.country == "mx"

    am_song = parse_music_link("https://music.apple.com/us/song/yellow/1122782283")
    assert am_song and am_song.service == "apple_music" and am_song.track_id == "1122782283"

    dz = parse_music_link("https://www.deezer.com/es/track/3135556")
    assert dz and dz.service == "deezer" and dz.track_id == "3135556"

    yt = parse_music_link("https://music.youtube.com/watch?v=yKNxeF4KMsY")
    assert yt and yt.service == "youtube" and yt.track_id == "yKNxeF4KMsY"

    yt_short = parse_music_link("https://youtu.be/yKNxeF4KMsY")
    assert yt_short and yt_short.service == "youtube" and yt_short.track_id == "yKNxeF4KMsY"

    td = parse_music_link("https://listen.tidal.com/track/12345678")
    assert td and td.service == "tidal" and td.track_id == "12345678"

    sc = parse_music_link("https://soundcloud.com/coldplay/yellow")
    assert sc and sc.service == "soundcloud" and sc.slug_artist == "coldplay" and sc.slug_title == "yellow"

    assert parse_music_link("yellow coldplay") is None
    assert parse_music_link("file:///C:/evil/local.mp3") is None


def test_resolve_apple_music_link_cross_matches_deezer(tmp_path):
    def handler(req):
        if req.url.host == "itunes.apple.com":
            return httpx.Response(200, json={
                "results": [{
                    "wrapperType": "track",
                    "trackId": 1122782283,
                    "trackName": "Yellow",
                    "artistName": "Coldplay",
                    "collectionName": "Parachutes",
                    "trackTimeMillis": 266773,
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/100x100bb.jpg",
                }]
            })
        if req.url.host == "api.deezer.com" and req.url.path == "/search/track":
            return httpx.Response(200, json={
                "data": [{
                    "id": 999,
                    "title": "Yellow",
                    "duration": 267,
                    "link": "https://www.deezer.com/track/999",
                    "artist": {"name": "Coldplay"},
                    "album": {"title": "Parachutes", "cover_xl": "https://cdn.deezer.com/999.jpg"},
                }]
            })
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    dz = DeezerClient(http=http, cache_dir=tmp_path)
    parsed = parse_music_link("https://music.apple.com/mx/album/parachutes/1122782080?i=1122782283")
    res = resolve_link_metadata(parsed, deezer=dz, http=http)
    assert res.title == "Yellow" and res.artist == "Coldplay" and res.album == "Parachutes"
    assert res.tracks and res.tracks[0].id == "999" and res.tracks[0].provider == "deezer"


def test_resolve_youtube_link_strips_video_tags(tmp_path):
    def handler(req):
        if req.url.host == "www.youtube.com" and req.url.path == "/oembed":
            return httpx.Response(200, json={
                "title": "Coldplay - Yellow (Official Video)",
                "author_name": "Coldplay - Topic",
            })
        if req.url.host == "api.deezer.com":
            return httpx.Response(200, json={
                "data": [{
                    "id": 999,
                    "title": "Yellow",
                    "duration": 267,
                    "link": "https://www.deezer.com/track/999",
                    "artist": {"name": "Coldplay"},
                    "album": {"title": "Parachutes"},
                }]
            })
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    dz = DeezerClient(http=http, cache_dir=tmp_path)
    res = resolve_link_metadata(parse_music_link("https://youtu.be/yKNxeF4KMsY"), deezer=dz, http=http)
    assert res.title == "Yellow" and res.artist == "Coldplay"
    assert res.tracks and res.tracks[0].title == "Yellow"
