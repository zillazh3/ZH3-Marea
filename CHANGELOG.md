# Changelog

All notable changes to ZH3 Marea are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).
Version 0.1.3 was an internal milestone that was never released; its work ships as part of 0.2.0.

## [0.2.1] - 2026-11-17

### Changed

- The window title bar shows the song playing on Spotify ("Title — Artist") when no project is open; an open project still takes priority,
  and the app name is shown when nothing is playing. The title is always rendered as plain text.

## [0.2.0] - 2026-11-17

### Added

- **English interface.** The whole UI is available in English and Spanish through JSON catalogs (`assets/i18n/en.json` and `assets/i18n/es.json`).
- **Automatic system-language detection.** On first launch, and on every launch while the setting is "System (auto)", the app reads the
  Windows preferred UI languages, matches by base language (es-MX, es-ES and es-AR to Spanish; en-US and en-GB to English) and falls back
  to English for unsupported languages.
- **Language selector** in Settings (System (auto) / Español / English). Switching re-translates the interface live, without a restart, and
  the choice is stored as `language` in `settings.ini`.
- `MAREA_LANG=es|en` environment variable to force a language for development and QA.
- Localization helper `marea/i18n.py` (`t(key, **params)`, `{param}` interpolation, plural forms with `count`, fallback order selected
  language, then English, then the key itself) and Qt glue `marea/ui/lang.py`.
- `tools/check_i18n.py` and `tests/test_i18n.py`: fail on missing or extra keys, placeholder mismatches, undefined or unused keys and
  dynamic keys. Adding a language only needs a new catalog and an entry in `SUPPORTED`.
- **Bilingual documentation:** `README.md` in English, `LEEME.md` in Spanish, with a language switcher and a Localization section, plus this changelog.
- **Own-video and transparent backgrounds.** Own-video backgrounds have a deterministic clip-time to frame mapping (trim, offset, repeat,
  ping-pong and hold loops), fit, zoom and pan, dim, blur, saturation, a readability scrim and a cover tint. The transparent background
  shows a checkerboard in the preview.
- **Alpha export:** WebM VP9, ProRes 4444 and PNG sequence with straight alpha, a JSON sidecar, thumbnails and history support; the
  self-test probes the alpha encoders.
- **Corrientes**, saveable and shareable style presets (`marea.preset` v1 JSON) with the built-in Bajamar, Pleamar, Niebla and Abisal looks,
  user presets with hot reload, import and export, partial apply, a "modified" indicator and thumbnails rendered by the real engine. The
  project remembers the applied preset.
- **Anzuelo**, a verse-range suggester for 15, 30, 60 s or custom-length clips (repeated line blocks, title mentions, vocal energy, onsets,
  section entries, word-effect density, intro, outro and mid-phrase penalties), with weights in an editable, hot-reloaded JSON, and
  queueing of selected ranges or one A/B variant per Corriente.
- **Encoder detection and codec choice:** every NVIDIA NVENC, AMD AMF, Intel Quick Sync and CPU backend and codec pair is probed with a real
  encode; an "Automatic" encoder (first working GPU, otherwise CPU) and a codec menu (H.264 by default, HEVC, AV1) are offered, with a
  one-time notice when the chosen GPU cannot encode the codec.
- **Library** page backed by a project registry (`library.json`) with an All / Projects / Clips filter, and a **Ctrl+K** command palette.
- **Semantic Word FX v2** (unreleased 0.1.3 work): FX configuration, per-category variants, staging, new animation primitives and an
  effects gallery.
- Preview pacing and lag **debug overlay (F3)**, and engine tuning constants (unreleased 0.1.3 work).
- The portable folder now ships `README.md`, `LEEME.md` and `CHANGELOG.md`; `assets/i18n` is bundled in the executable.

### Changed

- The portable build is versioned: `ZH3-Marea-v0.2.0-portable.zip` (built by `tools/build_exe.py --zip`), published with `SHA256SUMS.txt`.
- Sidebar reordered like the web app, with the Spotify connection status above the Settings button; Home rebuilt with a hero card, now playing and recent projects, and Settings laid out as cards.
- The Editor export bar shows export-mode chips and the format picker; encoder and output folder live only in Settings.
- Corrientes cards are compact (4:5 cropped thumbnails with hover and selected states, elided names), section checkboxes moved into the
  options menu, and the editor panel is wider so all six tabs fit.
- Unnamed push buttons (dialogs, Anzuelo chips) get the Ghost look; the default dialog button uses the accent gradient.
- Wider local audio offset range, vocal-stem aware offset detection, the audio offset resets when new lyrics are loaded, and the glow
  curve keeps building on held notes (unreleased 0.1.3 work).
- The Spanish README moved to `LEEME.md`; the new `README.md` is the English one.
- Documentation covers the video background, alpha export formats, Corrientes and Anzuelo, and credits the libvpx, libopus, prores_ks,
  libx265 and SVT-AV1 codecs in `THIRD_PARTY.md`.

### Fixed

- Corrientes thumbnails never rendered: Skia's PNG encoding without a quality argument raised a `TypeError` that the async error callback swallowed.
- Word FX: the audio offset is now applied to energy and strain, anticipation shares the line lookahead constant, carrier letters align
  with the base scale, carrier x shift is never clipped, FX glow uses the tuning gain, and the sung-only bloom is cached.
- Audio alignment and engine timing hardening (unreleased 0.1.3 work).
- Documentation: the Word FX category count (55: 23 emotions, 16 elements, 4 seasons, 12 months), the test count, and the portable ZIP name.

### Security

- Corriente import is validated against a whitelisted, clamped schema: files larger than 256 KB are rejected before they are read, unknown
  keys are ignored, and a missing font is replaced with Inter Tight.
- Own-video backgrounds are referenced by path only and never stored inside a Corriente; if the video is missing when a project opens, the
  app falls back to the cover art instead of failing.
- Network (UNC) and device paths found in an opened project (audio, background video, cinematic videos) or in a Lab alignment file are
  ignored, so opening a shared file can no longer make Windows contact a remote share and leak the user's NTLM credentials.
- Credentials are unchanged: keys and tokens stay in the Windows Credential Manager, never in files.

## [0.1.2] - 2026-11-14

See the [GitHub release notes](https://github.com/zillazh3/ZH3-Marea/releases/tag/v0.1.2).

[0.2.1]: https://github.com/zillazh3/ZH3-Marea/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/zillazh3/ZH3-Marea/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/zillazh3/ZH3-Marea/releases/tag/v0.1.2
