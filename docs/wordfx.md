# Semantic Word FX (experimental)

Individual words in a lyric line react to their **meaning and emotion**, only inside the word: in a line about wasted time,
*tiempo* feels like time passing and *perdido* sinks and crumbles. Nothing is a fixed per-word animation. Each word is mapped to
semantic categories, each category carries an **emotion vector**, and the engine composes the effect for every occurrence from that
vector plus its context (neighbors, line, repetition, how long the word is held, the audio).

Enable it in **Settings → Experimental features → Semantic word effects**. It is off by default; with it off nothing from
`marea.wordfx` is imported. The Editor's *Style* tab then shows the **FX intensity** slider and **Debug word effects**.

Preview and export use the same pre-composed plan and every random value is a pure function of
`(track id, line text, word, letter, time)`, so a recorded clip is pixel-identical to what you saw.

## Where the pieces live

| File | What it is |
|---|---|
| `assets/wordfx/library.json` | Vocabulary: categories, lemmas, vectors, primitives (bundled default) |
| `assets/wordfx/rules.json` | Context rules: phrases, negation, reversal, POS guards, contrast pairs |
| `marea/wordfx/config.py` | **Every look tunable** (`FX`): glow, micro light streak, staging, motion clamps, envelope |
| `marea/wordfx/variants.py` | Named variants per category (2-3 each), picked deterministically |
| `marea/wordfx/signatures.py` | Base motion signature of every category (waveform, axis, glow, particles, release) |
| `<app data>/wordfx/library.json`, `rules.json` | Your copies. If present they **replace** the bundled ones |
| `<app data>/wordfx/songs/<track id>.json` | Per-song overrides (track id = Spotify id; TTML projects use a hash of the lyrics) |

`<app data>` is `data/` next to the `.exe` (portable) or `%LOCALAPPDATA%\ZH3 Marea`. Files are **hot-reloaded**: while the feature
is on, the Editor re-reads them when their modification time changes (about once a second). A file with an error keeps the previous
plan and shows the error; fix the JSON and it recovers.

## Adding lemmas and categories

A category in `library.json`:

```json
"sadness": {
  "kind": "emotion",
  "lemmas": { "es": ["llorar", "lágrima", "dolor"], "en": ["cry", "tear", "pain"] },
  "vector": { "valence": -0.8, "arousal": 0.3, "gravity": 0.8, "temperature": -0.5, "fragility": 0.5, "blur": 0.3 },
  "primitives": ["gravity_drift", "desaturate", "blur", "particles"],
  "priority": 7
}
```

- **Lemmas are dictionary forms**: infinitive for verbs (`llorar`, not `lloré`), singular for nouns, masculine singular for
  adjectives. The matcher lowercases, strips accents and lemmatizes each lyric word (`perdido`, `perdí`, `perdiendo` → `perder`),
  so you never list conjugations. English is lemmatized the same way (`lost` → `lose`).
- Language is detected **per word**, so English words inside a Spanish line (`again`, `high`, `loop`) match the English list.
- Multi-word lemmas (`para siempre`, `let go`, `a ciegas`) are matched as phrases.
- A lemma can belong to several categories (`llorar`: sadness + water). Their vectors are blended by `priority`; `emotion`
  categories weigh 1.5× more than `element` ones.
- Keep lemmas as content words. Ambiguous ones (`solo`, `vino`, `still`) belong in `rules.json` guards.
- Only vocabulary goes in these files, never song lyrics.

## Tuning vectors

All axes are in `vector`. The composer maps them to parameters, but **only through the primitives the category lists**.

| Axis | Range | Drives |
|---|---|---|
| `valence` | -1 … 1 | brightness, glow, warm vs. dull color |
| `arousal` | 0 … 1 | jitter amplitude and speed, wave speed, saturation, stagger speed |
| `gravity` | -1 … 1 | vertical drift: positive sinks, negative rises |
| `temperature` | -1 … 1 | hue (cold steel/cyan/lavender to warm amber/pink/crimson) |
| `fragility` | 0 … 1 | particles, erosion, fracture, lower opacity |
| `blur` | 0 … 1 | initial blur that clears over the word |

Primitives: `displacement`, `gravity_drift`, `letter_offset`, `mask_reveal`, `scale`, `rotation`, `opacity`, `blur`,
`letter_spacing`, `jitter`, `wave`, `glow`, `hue_shift`, `desaturate`, `chromatic`, `grain`, `particles`, `erode`, `fracture`,
`trail`, `sparkle`. Every FX word also gets a soft halo in its own color so it stands out from its neighbors.

Color comes from the valence × temperature anchors, scaled by arousal and valence, then blended **60/40 with the cover's dominant
colors** and lifted to a minimum luminance so it stays readable on the blurred background.

How an occurrence is composed: its categories, bleed from FX words within 2 tokens (30%), line mood (15%), song mood (15%), how long
the word is held (longer holds erode and drift more). Repetition does **not** change the look: every random choice is a pure
function of `(track id, normalised line text, word index)`, so a chorus that comes back looks exactly like its first time. The
debug overlay still counts repetitions (`rep N`).

## How a word looks (v2)

The Apple Music look is the base; the FX is a layer on top and **the word returns exactly to the plain Apple Music state after its
release** (`residue_gain = 0`, no leftover tint, the opacity breathing stops with the word). Everything moves with transform, opacity,
colour and glow only: no FX ever changes the layout.

**Lifecycle** (tied to the word's own timing): a tiny anticipation (a 1 px counter-dip, strong words only) → attack at the word's start
(the letters stretch and squash by up to 3 %, area kept) → sustain (follows the fill, short words get a quick small accent, held
words a slow build) → release (eased, 0.4-0.9 s). Letters peak one after another (follow-through), drifts follow small arcs.

**Staging and density.** At most `max_fx_per_line` (2) words per line get an FX; of those only `max_strong_per_line` (1, the highest
scoring; contrast pairs stay together) get the **strong** version. The others are **light**: `light_scale` of the amplitude, no
particles, sparkles, chromatic split, streak, glitch, shimmer or scatter, and never more glow than the base.

**Variants.** Every category has 2-3 looks (`base` plus its variants in `variants.py`; a variant only overrides some fields of the
category's signature). The pick is a pure function of the song, the **normalised line text** and the word index, so a chorus that
comes back looks identical, and the previous FX word of the line is excluded so neighbours never repeat a variant (when the
category has a choice). The debug overlay prints `category.variant`. `variant_weights` in the config re-weights them.

**Glow.** Per-category gain (`glow_by_category`): bright categories (light, celestial, fire, electric, joy, love, hope, summer) are
+30-50 %, calm/dark ones (sadness, loneliness, shadow, fear, death, numbness, loss) at or below the base glow, anything unlisted
is 1.0. It follows the fill (unsung letters do not glow), builds from `glow_build_start` to its peak at the end of the word and
settles with the release, and drops to `glow_bright_floor` on a bright background so the text stays legible. Words without FX keep
the standard Apple Music glow.

**Micro light streak.** A very thin, faint, near-white line through the *middle* of the letters (the x-height centre, never under
them), anchored to the fill head: brightest at the head, a short taper in front, a longer trail behind that dissolves. It appears
with the attack, travels with the fill and fades out completely in the release. It may overshoot the word by `streak_overshoot_em`
and is the only thing drawn outside the sung area. Strength is per category (`streak_by_category`: strong on light, celestial,
electric, fire, joy, love, hope; faint on sadness; off on fear, shadow and on words without FX). Thickness is `streak_thickness_px`
at 1080p and scales with the output size. Light words and reduced motion draw no streak.

**Primitives** added in v2 (all deterministic): `heartbeat` and `shake` waveforms (1-2 beats / a decaying shake from the attack),
`arc` axis (sideways drift along an arc), `shimmer` (light sweeping across the word with the fill), `scatter` (letters drift out on
an arc and reassemble), `tint_spread` = `frost` (left to right) or `ink` (from the centre), `glitch` (rare, sustain only, electric
words), and the `ripple` motif (a thin line rippling *under* the word, unrelated to the streak).

**New categories:** hope, loneliness, desire, freedom, light, electric, cold, death, scream, whisper, breath (Spanish and English;
night/darkness extend `shadow`, sky/moon `celestial`, nostalgia `memory`, heart `love`, flowers `spring`).
Classification fixes: `solo`/`sola` no longer trigger the sun (they mean *alone* only in clear contexts), `sol` as a music note,
`rose`/`rosa` as verb/colour, Spanish `seas` and English `live` are guarded; `burning`/`burnt` resolve like `burn`.

**Tuning.** All numbers live in `marea/wordfx/config.py`, commented. Start with `glow_gain`, `glow_by_category`,
`streak_opacity` / `streak_thickness_px` / `streak_trail_em` / `streak_by_category`, `stretch_amt`, `max_strong_per_line`,
`light_scale` and `variant_weights`. The **FX Gallery** (Editor menu → *Galería de efectos…*, or
`python -m marea.ui.wordfx_gallery`) previews every category and variant on Spanish and English words in a loop, with sliders for
intensity, glow and the streak, and copies the changed values as a config snippet.

## Context rules (`rules.json`)

Order of application: **phrases → POS guards → reversal → negation → contrast → density limit (max 2 FX words per line, 1 strong)**.
Keys starting with `_` are comments. Categories that do not exist in your library are ignored.

- `phrases`: `{ "pattern": "otra vez", "lang": "es", "categories": ["longing"] }`. Consecutive words (forms or lemmas), `*` matches
  exactly one word. A phrase overrides its single words and places its effect on its **last** word, or on the word given by
  `"at"` (0-based index inside the pattern: `{ "pattern": "mundo acaba hoy", ..., "at": 1 }` puts it on *acaba*).
- `negators` + `negation`: a negator up to 2 words before a matched word replaces its categories with the table entry; `[]` cancels
  the effect. `sin miedo` → growth, `no puedo respirar` → anguish.
- `reversals`: `{ "trigger": ["volver", "a"], "lang": "es", "map": { "calm": ["healing"] } }`. `volví a respirar` → healing. `side`
  is `before` (default) or `after`.
- `pos_guards`: `{ "word": "vino", "lang": "es", "keep_if": {...}, "drop_if": {...} }`. Conditions are `prev`, `next` (form or lemma
  of the neighbor), `line_end`, `line_start`. `drop_if` wins over `keep_if`.
- `contrast`: `categories` (pairs of opposite categories) and `words` (pairs of lemmas, e.g. `cura`/`veneno`). Two opposite words in
  one line are **tension**: both are kept, linked, and their color oscillates at ~2 Hz between the two poles while the second pole
  trembles.

## Background-aware color

Before drawing a word, the renderer samples the **real background under it** (blurred cover or cinematic image/video, from a small
thumbnail refreshed 10 times per second) and corrects the word's colors so they keep a contrast of at least 3:1, **without changing
their hue**: over a dark background a color is lightened (toward white), over a light one it is darkened (same hue and saturation,
less light). A color that already contrasts is left alone. It is a pure function of the frame, so preview and export match.

The correction is **continuous**: it fades in as the contrast drops from 3.5:1 to 2.5:1, and the switch between "lighten" and "darken"
cross-fades over a band of background luminance (0.15–0.29), so the color never jumps as the background drifts.

Turn it on or off with **Adapt effect colors to the background** in the Editor's *Style* tab (on by default, remembered between sessions).

Limits: the sample is the average color under the word's box, not per pixel. Inside the cross-fade band (mid-gray backgrounds) neither
lighter nor darker reaches 3:1, so a word is briefly less contrasted there; that is the price of not jumping. Normal (non-effect)
lyrics are not touched.

## Per-song overrides

`<app data>/wordfx/songs/<track id>.json`:

```json
{"overrides": [
  {"line": 3, "word": 2, "disable": true},
  {"line": 3, "word": 2, "categories": ["sadness"], "vector": {"arousal": 0.9}, "intensity": 1.3},
  {"lemma": "tiempo", "disable": true}
]}
```

`line` and `word` count from 0 (the line in the lyrics and the word in that line). The debug overlay prints them as `[line.word]`
on every FX word; to force a word that has no effect yet, count its position in the line. `lemma` targets every occurrence. `categories` forces or replaces categories (even on a word that was not matched),
`vector` replaces individual axes of the final vector, and `intensity` (0–3) scales that one word. An override with `line`/`word`
beats one with `lemma`; among equals the last one in the file wins.

## Song mood profile

The whole song gets a profile that every effect adapts to:

```json
{"track_id": "...", "tags": ["sad", "anguished", "raw_vocals"],
 "arc": [{"start": 0.0, "end": 42.5, "mood": "nostalgic"}, {"start": 42.5, "end": 90.0, "mood": "joyful"}],
 "intensity": 0.85, "notes": "context"}
```

Tags: `sad`, `nostalgic`, `longing`, `anguished`, `tender`, `hopeful`, `joyful`, `euphoric`, `bittersweet`, `tribute`, `healing`,
`numb`, `raw_vocals`, `calm`. The arc is a list of contiguous sections (seconds) with one mood each.

It is computed locally and offline from:

- **Lyrics**: the emotion categories of the words, per line and for the whole song.
- **Audio** (needs a local audio file): tempo, energy, spectral brightness and major/minor mode (a minor key leans sad or nostalgic, a
  major one joyful or hopeful).
- **Vocal strain**: roughness, high-frequency energy and loudness peaks. It uses the separated lead vocal when the experimental
  vocal separation already cached it for this file, and otherwise falls back to the full mix. A strain of 0.6 or more adds `raw_vocals`.
- **Notes**: free text per song. Keywords bias the tags (family, parents, tribute → `tribute`; Alzheimer, memory → `nostalgic` +
  `tender`), and tag names written in the notes force them.

How it drives the effects:

- The mood of the moment (crossfaded over ~1 s at each arc boundary) is a baseline blended into every word's vector (15%): a sad
  section is cooler and heavier, a joyful one warmer and lighter.
- With `raw_vocals`, words of anguish or fear (and any word with jitter or chromatic aberration) follow the live strain curve:
  stronger jitter, ~2 px of chromatic aberration and extra glow on strained peaks.

Edit it in the Editor menu → **Song profile (word effects)**: toggle tags, change moods, move boundaries, split or merge sections,
write notes and press **Recalculate** to rebuild it from the lyrics, audio and notes. Saving writes the `"profile"` key of the song
file (next to its overrides); until you save, the profile is recomputed automatically and not stored.

## Debug overlay

*Debug word effects* underlines every FX word and labels it with `[line.word]`, lemma, language, categories, repetition count, the rule
that decided it (`phrase:…`, `neg:…`, `pair:…`, `override`), the final vector, bleed and the active primitives. It exists only in
the preview and never appears in an export.

## Known limits

- Reduced motion keeps color, opacity and glow but removes motion, particles and oscillation.
- Audio reactivity, vocal strain and the audio part of the profile need a local audio file; with a Spotify loopback source the audio is only captured at export time.
- The vocal-strain thresholds on the full mix were calibrated with synthetic signals; check them on real songs and use the profile editor to correct `raw_vocals`.
- Phrases attach to their last word, and the audio offset is not applied to the energy and strain curves.
- A phrase applies its effect to its last word only.
- Negation and guards look at neighboring words, not at a real part-of-speech tagger.
