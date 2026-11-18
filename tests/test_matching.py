"""Unit tests for query normalization, fuzzy track scoring, version penalties, and ambiguity detection."""
from marea.providers.matching import (
    clean_search_query,
    clean_youtube_title,
    needs_disambiguation,
    normalize_text,
    rank_tracks,
    score_track,
    split_title_and_metadata,
)
from marea.providers.track import Track


def _track(tid: str, title: str, artists: list[str], duration_ms: int = 200_000, provider: str = "deezer") -> Track:
    return Track(tid, title, artists, "Album", duration_ms, None, provider=provider)


def test_normalize_handles_accents_punctuation_and_case():
    assert normalize_text("¡Corazón, Dónde Estás? (En Vivo)") == "corazon donde estas en vivo"
    assert normalize_text("Don't Stop Me Now — Remastered") == "dont stop me now remastered"


def test_split_title_extracts_features_and_version_tags():
    base, feat, tags = split_title_and_metadata("Despacito (feat. Daddy Yankee) [Remix]")
    assert base == "despacito"
    assert feat == ["daddy yankee"]
    assert tags == {"remix"}

    base2, feat2, tags2 = split_title_and_metadata("Blinding Lights - Remastered 2023")
    assert base2 == "blinding lights" and feat2 == [] and tags2 == set()


def test_fuzzy_ranking_tolerates_typos_accents_incomplete_and_collabs():
    studio = _track("1", "Tití Me Preguntó", ["Bad Bunny"])
    typo_target = _track("2", "La Canción", ["J Balvin", "Bad Bunny"])
    other = _track("3", "Callaita", ["Bad Bunny"])

    r1 = rank_tracks([other, studio], query_text="titi me pregnto bad buny")
    assert r1[0].track.id == "1" and r1[0].match.score > 0.75

    r2 = rank_tracks([other, typo_target], query_title="La Cancion", query_artist="Bad Bunny feat. J Balvin")
    assert r2[0].track.id == "2" and r2[0].match.score >= 0.85

    # Incomplete song title
    r3 = rank_tracks([other, studio], query_title="Titi Me", query_artist="Bad Bunny")
    assert r3[0].track.id == "1" and r3[0].match.title_score >= 0.85


def test_version_penalties_prefer_studio_unless_queried():
    studio = _track("1", "Yellow", ["Coldplay"])
    live = _track("2", "Yellow (Live in Buenos Aires)", ["Coldplay"])
    remix = _track("3", "Yellow - Club Remix", ["Coldplay"])
    cover = _track("4", "Yellow (Acoustic Cover)", ["Vitamin String Quartet"])

    ranked = rank_tracks([cover, remix, live, studio], query_title="Yellow", query_artist="Coldplay")
    assert ranked[0].track.id == "1"
    assert all(r.match.penalty > 0 for r in ranked[1:])

    # When the user explicitly asks for Live, live should not be penalized and should win.
    ranked_live = rank_tracks([studio, live], query_title="Yellow (Live)", query_artist="Coldplay")
    assert ranked_live[0].track.id == "2" and ranked_live[0].match.penalty == 0


def test_needs_disambiguation_flags_uncertain_or_tied_matches():
    strong = [_track("1", "Yellow", ["Coldplay"]), _track("2", "Yellow (Live)", ["Coldplay"])]
    assert not needs_disambiguation(rank_tracks(strong, query_title="Yellow", query_artist="Coldplay"))

    weak = [_track("1", "Completely Different Song", ["Someone"])]
    assert needs_disambiguation(rank_tracks(weak, query_title="Yellow", query_artist="Coldplay"))

    ambig = [_track("1", "Stay", ["The Kid LAROI"]), _track("2", "Stay", ["Zedd"])]
    assert needs_disambiguation(rank_tracks(ambig, query_text="Stay"))


def test_clean_helpers():
    assert clean_search_query("Hello (feat. Adele) [Official Audio]") == "hello adele"
    title, artist = clean_youtube_title("Coldplay - Yellow (Official Video)", "Coldplay - Topic")
    assert (title, artist) == ("Yellow", "Coldplay")
    assert score_track(query_title="A", query_artist="B", query_duration_ms=200000,
                       cand_title="A", cand_artists=["B"], cand_duration_ms=200500).duration_score == 1.0
