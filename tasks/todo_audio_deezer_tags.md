# ZH3 Marea: Audio Preview, Deezer Fallback, Link Analyzer & Source Tags

Test command: `.venv/Scripts/python -m pytest tests/test_project.py tests/test_library.py tests/test_lyrics.py tests/test_providers.py tests/test_spicy.py tests/test_audio.py tests/test_audio_timeline.py tests/test_offset.py tests/test_editor.py tests/test_preview_debug.py tests/test_spotify_ui.py tests/test_ui.py tests/test_i18n.py tests/test_i18n_ui.py tests/test_unc_paths.py -q`

## Phase 1: Foundations & Contracts
- [x] Task 1: Provider-agnostic `Track`, canonical lyrics source tags, `PROJECT_VERSION = 2` migration, and `library.json` persistence
- [x] Task 2: Query normalization, fuzzy relevance ranking, and version penalties (`marea/providers/matching.py`)
- [x] Checkpoint A: Foundation models, v2 schema migration, and matching unit tests pass

## Phase 2: Real-Time Audio Preview
- [x] Task 3: Non-blocking streaming audio player & shared DAC-compensated clock (`marea/audio/player.py`)
- [x] Task 4: Sync `PreviewWidget`, millisecond readout (`M:SS.mmm`), Stop/Volume/Mute controls, `ClickSeekSlider`, live offset shift
- [x] Task 5: Interactive `WaveformWidget` playhead and click/drag scrubbing
- [x] Checkpoint B: Audio preview, seeking, live offset, and editor/export tests pass

## Phase 3: Deezer Fallback, Link Analyzer & Candidate Disambiguation
- [x] Task 6: `DeezerClient`, `LrclibProvider` fuzzy upgrade, and strict `ChainProvider` validation & fallback
- [x] Task 6b: Music link analyzer & metadata resolver (`marea/providers/links.py`, `SpotifyClient.track`)
- [x] Task 7: `Services` fallback & link resolution, un-gated `SearchPage`, `CandidateDialog`, Editor metadata editing & loopback guard
- [x] Checkpoint C: Deezer search/metadata fallback, link parsing, fuzzy ranking, and disambiguation pass

## Phase 4: Native Lyrics Source Tags & Verification
- [x] Task 8: Native `SourceTagButton` (`Spicy Lyrics`, `LRCLIB`, `Manual`, `Unknown`), `theme.py` tokens, Editor inspector & retry menu
- [x] Task 9: Source tags in Search, Library & Home + i18n catalogs (`en.json`, `es.json`) + `check_i18n.py`
- [x] Checkpoint D (Final): Full verification & walkthrough
