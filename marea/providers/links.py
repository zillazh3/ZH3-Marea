"""Music link parser and metadata resolver for Spotify, Apple Music, Deezer, YouTube, Tidal, and SoundCloud."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from marea.providers.matching import clean_youtube_title
from marea.providers.track import Track

_SPOTIFY_URI_RE = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_DIGITS_RE = re.compile(r"^\d+$")

_ALLOWED_SHORT_HOSTS = frozenset({"spotify.link", "spoti.fi", "link.deezer.com", "deezer.page.link", "youtu.be"})
_ALLOWED_LOOKUP_HOSTS = frozenset({
    "open.spotify.com",
    "music.apple.com",
    "itunes.apple.com",
    "www.deezer.com",
    "deezer.com",
    "youtube.com",
    "www.youtube.com",
    "music.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "tidal.com",
    "listen.tidal.com",
    "soundcloud.com",
    "www.soundcloud.com",
    "m.soundcloud.com",
})


@dataclass(frozen=True)
class ParsedLink:
    service: str  # "spotify" | "apple_music" | "deezer" | "youtube" | "tidal" | "soundcloud"
    kind: str  # "track" | "album" | "short"
    raw_url: str
    track_id: str = ""
    album_id: str = ""
    country: str = ""
    slug_title: str = ""
    slug_artist: str = ""


@dataclass
class LinkResolution:
    link: ParsedLink
    direct_track: Track | None = None
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    cover_url: str | None = None
    tracks: list[Track] = field(default_factory=list)


def _slug_to_words(slug: str) -> str:
    return unquote(slug or "").replace("-", " ").strip()


def _normalize_url(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("http://"):
        return "https://" + s[7:]
    return s


def parse_music_link(text: str) -> ParsedLink | None:
    """Parse a pasted music URL or URI into a `ParsedLink`, or return None if plain search text."""
    raw = (text or "").strip()
    if not raw or " " in raw:
        return None
    if m := _SPOTIFY_URI_RE.fullmatch(raw):
        return ParsedLink("spotify", "track", raw, track_id=m.group(1))

    norm = _normalize_url(raw)
    if not norm.startswith("https://"):
        return None
    u = urlparse(norm)
    host = (u.hostname or "").lower()
    if not host:
        return None
    parts = [p for p in u.path.split("/") if p]
    qs = parse_qs(u.query)

    # Spotify
    if host == "open.spotify.com":
        if parts and parts[0].startswith("intl-"):
            parts = parts[1:]
        if parts and parts[0] == "embed":
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "track" and _SPOTIFY_ID_RE.fullmatch(parts[1]):
            return ParsedLink("spotify", "track", norm, track_id=parts[1])
        if len(parts) >= 2 and parts[0] == "album":
            return ParsedLink("spotify", "album", norm, album_id=parts[1])
        return None
    if host in ("spotify.link", "spoti.fi") and parts:
        return ParsedLink("spotify", "short", norm)

    # Apple Music / iTunes
    if host in ("music.apple.com", "itunes.apple.com"):
        country = parts[0] if parts and len(parts[0]) == 2 else ""
        rest = parts[1:] if country else parts
        if len(rest) >= 3 and rest[0] == "album" and _DIGITS_RE.fullmatch(rest[2]):
            tid = (qs.get("i") or [""])[0]
            if tid and _DIGITS_RE.fullmatch(tid):
                return ParsedLink("apple_music", "track", norm, track_id=tid, album_id=rest[2],
                                  country=country, slug_title=_slug_to_words(rest[1]))
            return ParsedLink("apple_music", "album", norm, album_id=rest[2],
                              country=country, slug_title=_slug_to_words(rest[1]))
        if len(rest) >= 3 and rest[0] == "song" and _DIGITS_RE.fullmatch(rest[2]):
            return ParsedLink("apple_music", "track", norm, track_id=rest[2],
                              country=country, slug_title=_slug_to_words(rest[1]))
        return None

    # Deezer
    if host in ("deezer.com", "www.deezer.com"):
        rest = parts[1:] if parts and len(parts[0]) == 2 else parts
        if len(rest) >= 2 and rest[0] == "track" and _DIGITS_RE.fullmatch(rest[1]):
            return ParsedLink("deezer", "track", norm, track_id=rest[1])
        if len(rest) >= 2 and rest[0] == "album" and _DIGITS_RE.fullmatch(rest[1]):
            return ParsedLink("deezer", "album", norm, album_id=rest[1])
        return None
    if host in ("link.deezer.com", "deezer.page.link") and parts:
        return ParsedLink("deezer", "short", norm)

    # YouTube / YouTube Music
    if host in ("youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"):
        vid = (qs.get("v") or [""])[0]
        if vid:
            return ParsedLink("youtube", "track", norm, track_id=vid)
        if len(parts) >= 2 and parts[0] in ("shorts", "embed"):
            return ParsedLink("youtube", "track", norm, track_id=parts[1])
        return None
    if host == "youtu.be" and parts:
        return ParsedLink("youtube", "track", norm, track_id=parts[0])

    # Tidal
    if host in ("tidal.com", "listen.tidal.com"):
        rest = parts[1:] if parts and parts[0] == "browse" else parts
        if len(rest) >= 2 and rest[0] == "track" and _DIGITS_RE.fullmatch(rest[1]):
            return ParsedLink("tidal", "track", norm, track_id=rest[1])
        return None

    # SoundCloud
    if host in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com") and len(parts) >= 2:
        if parts[0] not in ("discover", "search", "you", "sets"):
            return ParsedLink("soundcloud", "track", norm,
                              slug_artist=_slug_to_words(parts[0]), slug_title=_slug_to_words(parts[1]))
    return None


def _is_safe_https_url(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and bool(u.hostname) and (u.hostname.lower() in _ALLOWED_LOOKUP_HOSTS or u.hostname.lower() in _ALLOWED_SHORT_HOSTS)


def _expand_short_link(link: ParsedLink, client: httpx.Client) -> ParsedLink:
    if link.kind != "short" or not _is_safe_https_url(link.raw_url):
        return link
    try:
        r = client.get(link.raw_url, follow_redirects=True, timeout=6.0)
        final_url = str(r.url)
        if _is_safe_https_url(final_url):
            parsed = parse_music_link(final_url)
            if parsed and parsed.kind != "short":
                return parsed
    except httpx.HTTPError:
        pass
    return link


def _itunes_lookup(track_id: str, country: str, client: httpx.Client) -> dict | None:
    params = {"id": track_id, "entity": "song"}
    if country:
        params["country"] = country
    try:
        r = client.get("https://itunes.apple.com/lookup", params=params, timeout=6.0)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        for item in results:
            if str(item.get("trackId") or "") == track_id or item.get("wrapperType") == "track":
                return item
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _oembed_lookup(endpoint: str, target_url: str, client: httpx.Client) -> dict | None:
    try:
        r = client.get(endpoint, params={"url": target_url, "format": "json"}, timeout=6.0)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        pass
    return None


def resolve_link_metadata(
    link: ParsedLink,
    *,
    spotify=None,
    deezer=None,
    http: httpx.Client | None = None,
) -> LinkResolution:
    """Resolve a `ParsedLink` into metadata hints and/or concrete `Track` candidates."""
    own_http = http is None
    client = http or httpx.Client(timeout=6.0, headers={"User-Agent": "ZH3 Marea/0.2"})
    try:
        resolved_link = _expand_short_link(link, client) if link.kind == "short" else link
        res = LinkResolution(link=resolved_link)

        if resolved_link.service == "spotify" and resolved_link.track_id:
            if spotify is not None and getattr(spotify, "connected", False):
                try:
                    t = spotify.track(resolved_link.track_id)
                    if t is not None:
                        res.direct_track = t
                        res.title, res.artist, res.album = t.title, t.artist, t.album
                        res.duration_ms, res.cover_url = t.duration_ms, t.cover_url
                        res.tracks = [t]
                        return res
                except Exception:
                    pass
            # Public Spotify oEmbed works without OAuth -> recover title/artist/cover, then search Deezer.
            oe = _oembed_lookup("https://open.spotify.com/oembed", f"https://open.spotify.com/track/{resolved_link.track_id}", client)
            if oe:
                res.title = str(oe.get("title") or "").strip()
                res.artist = str(oe.get("author_name") or "").strip()
                res.cover_url = oe.get("thumbnail_url") or None

        elif resolved_link.service == "deezer" and resolved_link.track_id and deezer is not None:
            t = deezer.track(resolved_link.track_id)
            if t is not None:
                res.direct_track = t
                res.title, res.artist, res.album = t.title, t.artist, t.album
                res.duration_ms, res.cover_url = t.duration_ms, t.cover_url
                res.tracks = [t]
                return res

        elif resolved_link.service == "apple_music" and resolved_link.track_id:
            item = _itunes_lookup(resolved_link.track_id, resolved_link.country, client)
            if item:
                res.title = str(item.get("trackName") or resolved_link.slug_title).strip()
                res.artist = str(item.get("artistName") or "").strip()
                res.album = str(item.get("collectionName") or "").strip()
                res.duration_ms = int(item["trackTimeMillis"]) if item.get("trackTimeMillis") else None
                art = str(item.get("artworkUrl100") or "")
                res.cover_url = art.replace("100x100bb", "600x600bb") if art else None
            else:
                res.title = resolved_link.slug_title

        elif resolved_link.service == "youtube":
            oe = _oembed_lookup("https://www.youtube.com/oembed", resolved_link.raw_url, client)
            if oe:
                title, artist = clean_youtube_title(str(oe.get("title") or ""), str(oe.get("author_name") or ""))
                res.title, res.artist = title, artist
                res.cover_url = oe.get("thumbnail_url") or None

        elif resolved_link.service == "soundcloud":
            oe = _oembed_lookup("https://soundcloud.com/oembed", resolved_link.raw_url, client)
            if oe:
                raw_title = str(oe.get("title") or "")
                author = str(oe.get("author_name") or resolved_link.slug_artist)
                if " by " in raw_title and not author:
                    raw_title, author = raw_title.rsplit(" by ", 1)
                res.title = raw_title.strip() or resolved_link.slug_title
                res.artist = author.strip()
            else:
                res.title = resolved_link.slug_title
                res.artist = resolved_link.slug_artist

        elif resolved_link.service == "tidal":
            res.title = resolved_link.slug_title
            res.artist = resolved_link.slug_artist

        # Cross-search Spotify (when connected) / Deezer using extracted hints.
        q_str = " ".join(p for p in (res.artist, res.title) if p).strip()
        if q_str:
            candidates: list[Track] = []
            if spotify is not None and getattr(spotify, "connected", False):
                try:
                    candidates = spotify.search(q_str)
                except Exception:
                    candidates = []
            if not candidates and deezer is not None:
                try:
                    ranked = deezer.search_ranked(
                        q_str,
                        title=res.title,
                        artist=res.artist,
                        duration_ms=res.duration_ms,
                    )
                    candidates = [r.track for r in ranked]
                except Exception:
                    candidates = []
            # If the link was a Spotify track URL (even offline/disconnected), tag Spotify ID on single high match?
            # Keep identities independent unless verified, so attach spotify_id only if link itself was Spotify track.
            if resolved_link.service == "spotify" and resolved_link.track_id:
                for c in candidates:
                    c.provider_ids.setdefault("spotify", resolved_link.track_id)
            res.tracks = candidates
        return res
    finally:
        if own_http:
            client.close()
