"""Query normalization, fuzzy track similarity scoring, and version-penalty ranking."""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
import unicodedata

from marea.providers.track import Track

_FEAT_RE = re.compile(r"\b(?:feat\.?|ft\.?|featuring|with|con|pres\.?|vs\.?)\b", re.IGNORECASE)
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|;|/|\bx\b|\by\b|\band\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b|\bcon\b)\s*", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*[\(\[\{]([^\)\]\}]*)[\)\]\}]\s*")
_DASH_SUFFIX_RE = re.compile(r"\s+[-–—]\s+([^–—-]+)$")
_NON_ALNUM_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

# Keywords identifying alternate versions that should be penalized unless explicitly queried.
_PENALIZED_TAGS: dict[str, tuple[str, ...]] = {
    "live": ("live", "en vivo", "ao vivo", "concert", "concierto", "tour", "unplugged", "session", "mtv"),
    "remix": ("remix", "rmx", "mix", "rework", "vip", "club edit", "mashup", "bootleg", "dj"),
    "cover": ("cover", "tribute", "tributo", "karaoke", "instrumental", "made famous", "originally by", "version"),
    "speed": ("sped up", "speed up", "nightcore", "slowed", "reverb", "8d", "chopped", "acapella", "a cappella"),
}
_NEUTRAL_TAGS = (
    "remaster", "remastered", "radio edit", "single version", "album version", "explicit",
    "clean", "deluxe", "bonus track", "official", "audio", "lyric", "lyrics", "video", "hd", "hq",
)
_PENALTY_WEIGHTS = {"live": 0.24, "remix": 0.28, "cover": 0.34, "speed": 0.30}


@dataclass(frozen=True)
class MatchScore:
    score: float
    title_score: float
    artist_score: float
    duration_score: float
    penalty: float
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RankedTrack:
    track: Track
    match: MatchScore


def normalize_text(text: str) -> str:
    """Fold Unicode accents, lowercase, strip punctuation, and collapse whitespace."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.casefold()
    # Unify typographic quotes/apostrophes before stripping punctuation.
    lowered = lowered.replace("’", "").replace("'", "").replace("`", "")
    cleaned = _NON_ALNUM_RE.sub(" ", lowered)
    return _SPACE_RE.sub(" ", cleaned).strip()


def _detect_tags(text: str) -> set[str]:
    norm = f" {normalize_text(text)} "
    found: set[str] = set()
    for category, phrases in _PENALIZED_TAGS.items():
        for phrase in phrases:
            if f" {phrase} " in norm:
                found.add(category)
                break
    return found


def _is_feat_segment(seg: str) -> bool:
    return bool(_FEAT_RE.search(seg))


def _is_version_segment(seg: str) -> bool:
    if _detect_tags(seg):
        return True
    norm = normalize_text(seg)
    return any(tok in norm for tok in _NEUTRAL_TAGS) or bool(re.search(r"\b(19|20)\d{2}\b", norm))


def split_title_and_metadata(title: str) -> tuple[str, list[str], set[str]]:
    """Return `(base_title, embedded_featured_artists, version_tags)`."""
    raw = (title or "").strip()
    if not raw:
        return "", [], set()
    tags = _detect_tags(raw)
    featured: list[str] = []

    def _consume_paren(m: re.Match) -> str:
        inner = m.group(1).strip()
        tags.update(_detect_tags(inner))
        if _is_feat_segment(inner):
            cleaned = _FEAT_RE.sub("", inner).strip()
            featured.extend(split_artists(cleaned))
            return " "
        if _is_version_segment(inner):
            return " "
        return f" {inner} "

    base = _PAREN_RE.sub(_consume_paren, raw)
    if m := _DASH_SUFFIX_RE.search(base):
        suffix = m.group(1).strip()
        tags.update(_detect_tags(suffix))
        if _is_feat_segment(suffix):
            featured.extend(split_artists(_FEAT_RE.sub("", suffix).strip()))
            base = base[:m.start()]
        elif _is_version_segment(suffix):
            base = base[:m.start()]
    # Strip trailing unparenthesized "feat. Artist" from title.
    if fm := _FEAT_RE.search(base):
        tail = base[fm.end():].strip()
        if tail:
            featured.extend(split_artists(tail))
        base = base[:fm.start()]
    return normalize_text(base), [a for a in featured if a], tags


def split_artists(artist: str) -> list[str]:
    """Split collaboration strings into normalized unique artist tokens."""
    if not artist:
        return []
    parts = [normalize_text(p) for p in _ARTIST_SPLIT_RE.split(artist)]
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def clean_search_query(text: str) -> str:
    """Produce a normalized search query tolerant to accents, punctuation, and extra tags."""
    base, featured, _ = split_title_and_metadata(text)
    if featured:
        return " ".join([base, *featured]).strip()
    return base or normalize_text(text)


def clean_youtube_title(title: str, channel: str = "") -> tuple[str, str]:
    """Clean YouTube/YT Music video titles like 'Artist - Song (Official Video)' -> (title, artist)."""
    raw = re.sub(r"\s*[-–—]\s*Topic$", "", (channel or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"[\(\[](?:official\s*(?:music\s*)?video|official\s*audio|lyric\s*video|lyrics?|audio|hd|4k|visualizer)[\)\]]",
                     "", title or "", flags=re.IGNORECASE).strip()
    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        return right.strip(), left.strip()
    return cleaned, raw


def _fuzzy_pair(a: str, b: str) -> float:
    """Combined string + token similarity in [0, 1], tolerant of typos and incomplete prefixes."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb) if (ta or tb) else 0.0
    # Incomplete-title tolerance: all query tokens appear in candidate, or query is a prefix.
    subset_bonus = 0.0
    if ta and ta.issubset(tb):
        subset_bonus = 0.78 + 0.20 * (len(ta) / len(tb))
    elif b.startswith(a) and len(a) >= 3:
        subset_bonus = 0.76 + 0.18 * (len(a) / len(b))
    return min(1.0, max(seq, 0.55 * seq + 0.45 * jaccard, subset_bonus))


def _score_artists(query_artists: list[str], cand_artists: list[str], query_text: str) -> float:
    if not query_artists:
        # Freeform query: check if any candidate artist appears inside the freeform query string.
        if not query_text or not cand_artists:
            return 0.5
        best = max(_fuzzy_pair(a, query_text) for a in cand_artists)
        q_tokens = set(query_text.split())
        hits = sum(1 for a in cand_artists if set(a.split()).issubset(q_tokens))
        if hits:
            return min(1.0, 0.85 + 0.15 * (hits / len(cand_artists)))
        return best
    if not cand_artists:
        return 0.0
    scores = [max(_fuzzy_pair(qa, ca) for ca in cand_artists) for qa in query_artists]
    # Primary artist carries most weight; collaborations still boost exact multi-artist matches.
    primary = scores[0]
    avg = sum(scores) / len(scores)
    return 0.7 * primary + 0.3 * avg


def score_track(
    *,
    query_text: str = "",
    query_title: str = "",
    query_artist: str = "",
    query_duration_ms: int | None = None,
    cand_title: str,
    cand_artists: list[str] | str,
    cand_duration_ms: int | None = None,
) -> MatchScore:
    """Compute a normalized relevance score in [0, 1] with version penalties."""
    q_raw = query_text or f"{query_title} {query_artist}".strip()
    q_tags = _detect_tags(q_raw)
    q_norm = normalize_text(query_text)

    if query_title:
        q_base, q_feat, q_t_tags = split_title_and_metadata(query_title)
        q_tags |= q_t_tags
    else:
        q_base, q_feat, _ = split_title_and_metadata(query_text)

    q_artists = split_artists(query_artist) if query_artist else []
    for fa in q_feat:
        if fa not in q_artists:
            q_artists.append(fa)

    c_base, c_feat, c_tags = split_title_and_metadata(cand_title)
    c_artist_list = split_artists(", ".join(cand_artists) if isinstance(cand_artists, list) else cand_artists)
    for fa in c_feat:
        if fa not in c_artist_list:
            c_artist_list.append(fa)

    if query_title:
        title_score = _fuzzy_pair(q_base or normalize_text(query_title), c_base or normalize_text(cand_title))
    else:
        # Freeform query ("artist song" or "song"): test both direct match and stripped artist tokens.
        cand_full = normalize_text(f"{c_base} {' '.join(c_artist_list)}")
        direct = _fuzzy_pair(q_base or q_norm, c_base)
        combined = _fuzzy_pair(q_norm, cand_full)
        # Subtract matched artist tokens from freeform query to recover pure title similarity.
        q_rem_tokens = [w for w in q_norm.split() if not any(w in a.split() for a in c_artist_list)]
        rem_title = _fuzzy_pair(" ".join(q_rem_tokens), c_base) if q_rem_tokens else 0.0
        title_score = max(direct, combined * 0.95, rem_title)

    artist_score = _score_artists(q_artists, c_artist_list, q_norm)

    duration_score = 1.0
    if query_duration_ms and cand_duration_ms:
        diff_s = abs(query_duration_ms - cand_duration_ms) / 1000.0
        if diff_s <= 2.0:
            duration_score = 1.0
        elif diff_s <= 5.0:
            duration_score = 0.85
        elif diff_s <= 12.0:
            duration_score = 0.55
        else:
            duration_score = max(0.0, 1.0 - diff_s / 60.0)

    extra_tags = c_tags - q_tags
    missing_tags = q_tags - c_tags
    penalty = sum(_PENALTY_WEIGHTS[tag] for tag in extra_tags) + 0.08 * len(missing_tags)
    reasons = tuple(sorted(extra_tags))

    if query_duration_ms and cand_duration_ms:
        raw_score = 0.54 * title_score + 0.34 * artist_score + 0.12 * duration_score
    elif q_artists:
        raw_score = 0.60 * title_score + 0.40 * artist_score
    else:
        raw_score = 0.68 * title_score + 0.32 * artist_score

    final = max(0.0, min(1.0, raw_score - penalty))
    return MatchScore(final, title_score, artist_score, duration_score, penalty, reasons)


def rank_tracks(
    tracks: list[Track],
    *,
    query_text: str = "",
    query_title: str = "",
    query_artist: str = "",
    query_duration_ms: int | None = None,
) -> list[RankedTrack]:
    """Score and sort Track candidates best-first while preserving tie stability."""
    ranked = [
        RankedTrack(
            track=t,
            match=score_track(
                query_text=query_text,
                query_title=query_title,
                query_artist=query_artist,
                query_duration_ms=query_duration_ms,
                cand_title=t.title,
                cand_artists=t.artists,
                cand_duration_ms=t.duration_ms or None,
            ),
        )
        for t in tracks
    ]
    ranked.sort(key=lambda r: (round(r.match.score, 6), r.match.title_score, r.match.artist_score), reverse=True)
    return ranked


def needs_disambiguation(ranked: list[RankedTrack], threshold: float = 0.78, min_margin: float = 0.08) -> bool:
    """Return True when the top candidate lacks high confidence or has a close runner-up."""
    if not ranked:
        return False
    top = ranked[0].match
    if top.score < threshold or top.penalty > 0:
        return True
    if len(ranked) > 1:
        second = ranked[1].match
        # Same provider or meaningfully different title/artist within tight margin -> ask user.
        if (top.score - second.score) < min_margin and second.score >= threshold - 0.05:
            t1, _, _ = split_title_and_metadata(ranked[0].track.title)
            t2, _, _ = split_title_and_metadata(ranked[1].track.title)
            if t1 != t2 or ranked[0].track.id != ranked[1].track.id:
                return True
    return False
