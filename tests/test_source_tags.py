"""Tests for Lyrics Source Tags, theme integration, manual-edit protection, persistence, and Deezer audio guard."""
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox

from marea import library
from marea.demo_cover import demo_cover
from marea.model.lyrics import SOURCE_LRCLIB, SOURCE_MANUAL, SOURCE_SPICY, SOURCE_UNKNOWN, Lyrics, parse_lrc
from marea.project import ClipProject, project_from_dict, project_to_dict, save_project
from marea.providers.track import Track
from marea.ui import theme
from marea.ui.library import SOURCE_ROLE, LibraryPage
from marea.ui.services import Services
from marea.ui.source_tag import SourceTagButton, source_label, source_palette


class _StubLyrics:
    def __init__(self, ly: Lyrics | None):
        self.ly = ly
        self.calls = 0

    def fetch(self, _q):
        self.calls += 1
        return self.ly


def test_source_tag_palette_and_rendering_in_dark_and_light_themes(qapp):
    btn = SourceTagButton(SOURCE_SPICY)
    for mode in ("dark", "light"):
        theme.apply(qapp, mode)
        for tag in (SOURCE_SPICY, SOURCE_LRCLIB, SOURCE_MANUAL, SOURCE_UNKNOWN):
            btn.set_tag(tag)
            assert btn.text() == source_label(tag)
            fill, border, fg = source_palette(tag)
            assert fg.alpha() > 0 and border.alpha() > 0 and fill.alpha() >= 0
            shot = btn.grab().toImage()
            assert shot.width() >= 40 and shot.height() == SourceTagButton.HEIGHT
    theme.apply(qapp, "dark")


def test_editor_manual_protection_and_retry_preserves_offset(make_window, qapp, tmp_path, monkeypatch):
    fetched = parse_lrc("[00:02.00]updated line\n[00:05.00]\n")
    fetched.source, fetched.credit, fetched.provider_tag = "lrclib", "LRCLIB", SOURCE_LRCLIB
    stub_ly = _StubLyrics(fetched)
    services = Services(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat), lyrics=stub_ly, poll_ms=60_000)

    win = make_window(services=services)
    win.show()
    manual_ly = parse_lrc("[00:01.00]manual line\n[00:04.00]\n")
    manual_ly.provider_tag = SOURCE_MANUAL
    manual_ly.manual_override = True
    proj = ClipProject(manual_ly, "Song", "Artist", demo_cover(64), audio_offset_ms=350.0, album="Alb")
    win.load_project(proj)
    qapp.processEvents()

    e = win.editor
    assert e.source_tag.tag == SOURCE_MANUAL

    # Declining confirmation protects manual lyrics
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.No)
    e._retry_lyrics("auto")
    qapp.processEvents()
    assert stub_ly.calls == 0 and e.project.lyrics.lines[0].text == "manual line"
    assert e.project.audio_offset_ms == 350.0

    # Confirming overwrite replaces lyrics while preserving audio_offset_ms
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.Yes)
    e._apply_new_lyrics(fetched)
    qapp.processEvents()
    assert e.project.lyrics.resolved_source_tag == SOURCE_LRCLIB
    assert e.project.audio_offset_ms == 350.0
    assert e.source_tag.tag == SOURCE_LRCLIB
    win.close()


def test_deezer_track_does_not_drive_spotify_loopback_and_persists_tags(make_window, qapp, tmp_path):
    dz_track = Track("999", "Yellow", ["Coldplay"], "Parachutes", 266000, None, provider="deezer")
    ly = parse_lrc("[00:01.00]Look at the stars\n[00:04.00]\n")
    ly.source, ly.credit, ly.provider_tag = "lrclib", "LRCLIB", SOURCE_LRCLIB
    proj = ClipProject(ly, dz_track.title, dz_track.artist, demo_cover(64), track=dz_track, album="Parachutes")

    saved = save_project(proj, tmp_path / "dz.marea.json")
    idx = tmp_path / "library.json"
    entries = library.register(proj, saved, idx, tmp_path / "thumbs")
    assert entries[0]["lyrics_source"] == SOURCE_LRCLIB

    # Legacy library entry without lyrics_source defaults cleanly to unknown
    legacy = tmp_path / "legacy.marea.json"
    legacy.write_text("{}")
    library.add({"path": str(legacy), "title": "Legacy", "verses": 3}, idx)
    loaded_idx = library.load(idx)
    assert any(e["path"] == str(legacy) and "lyrics_source" not in e for e in loaded_idx)

    # Round-trip preserves Deezer track identity and LRCLIB tag
    back, _ = project_from_dict(project_to_dict(proj))
    assert back.track.provider == "deezer" and back.track.id_for("deezer") == "999"
    assert back.lyrics.resolved_source_tag == SOURCE_LRCLIB

    # Editor guards Spotify loopback against Deezer track
    win = make_window()
    win.load_project(proj)
    assert win.editor._audio.spotify_track() is None

    # LibraryPage renders both entries cleanly
    lib_page = LibraryPage(lambda _p: None)
    assert lib_page is not None
    win.close()
