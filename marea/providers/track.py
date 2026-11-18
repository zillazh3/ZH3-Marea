"""Provider-agnostic track identity shared across Spotify, Deezer, and project persistence."""
from dataclasses import dataclass, field

VALID_PROVIDERS = ("spotify", "deezer")


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    cover_url: str | None
    uri: str = ""
    provider: str = "spotify"
    provider_ids: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.id = str(self.id or "")
        self.provider = (self.provider or "spotify").strip().lower()
        clean: dict[str, str] = {}
        for k, v in (self.provider_ids or {}).items():
            if k and v:
                clean[str(k).strip().lower()] = str(v)
        if self.id and self.provider and self.provider not in clean:
            clean[self.provider] = self.id
        self.provider_ids = clean

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    def id_for(self, provider: str) -> str:
        key = provider.strip().lower()
        if self.provider == key and self.id:
            return self.id
        return self.provider_ids.get(key, "")

    @classmethod
    def from_api(cls, item: dict) -> "Track":
        """Build a Spotify Track from a Spotify Web API track object."""
        images = sorted(item.get("album", {}).get("images") or [], key=lambda i: i.get("width") or 0, reverse=True)
        tid = str(item["id"])
        return cls(
            id=tid,
            title=item["name"],
            artists=[a["name"] for a in item.get("artists", [])],
            album=item.get("album", {}).get("name", ""),
            duration_ms=item.get("duration_ms", 0),
            cover_url=images[0]["url"] if images else None,
            uri=item.get("uri", ""),
            provider="spotify",
            provider_ids={"spotify": tid} if tid else {},
        )

    @classmethod
    def from_deezer(cls, item: dict) -> "Track":
        """Build a Deezer Track from a Deezer API track object."""
        tid = str(item["id"])
        artist_obj = item.get("artist") or {}
        artists = [str(artist_obj["name"])] if artist_obj.get("name") else []
        for contrib in item.get("contributors") or []:
            cname = str((contrib or {}).get("name") or "").strip()
            if cname and cname not in artists:
                artists.append(cname)
        album_obj = item.get("album") or {}
        cover_url = (
            album_obj.get("cover_xl")
            or album_obj.get("cover_big")
            or album_obj.get("cover_medium")
            or album_obj.get("cover_small")
            or None
        )
        duration_s = item.get("duration") or 0
        return cls(
            id=tid,
            title=str(item.get("title") or ""),
            artists=artists,
            album=str(album_obj.get("title") or ""),
            duration_ms=round(float(duration_s) * 1000),
            cover_url=cover_url,
            uri=str(item.get("link") or ""),
            provider="deezer",
            provider_ids={"deezer": tid} if tid else {},
        )
