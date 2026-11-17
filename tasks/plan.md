# Implementation Plan: Semantic Word FX v2

Full design, audit table and risks: the original local planning file (approved).
Task checklist: `tasks/todo.md`.

## Overview
Improve the existing semantic word FX (`marea/wordfx/`) without touching lyric timing, scrolling or line layout:
one FX config, variants as composable layers, staging/density, per-category glow, micro light streak, refined and new
categories, better ES/EN classification, and an FX Gallery debug window. The Apple Music base look is preserved; after the
release phase a word renders exactly like a normal word.

## Decisions (from user)
- Repeated chorus lines get identical FX: seed = hash(song + normalized line text + word index). Repeat escalation removed.
- Do not start implementing until the timing/sync task's uncommitted edits are committed (overlapping files:
  `marea/render/scene.py`, `marea/wordfx/{render,anim,envelope,plan}.py`, `marea/engine/tuning.py`).

## Architecture
- `marea/wordfx/config.py`: single commented config (`FX`, `set_fx`).
- `marea/wordfx/variants.py`: 2-3 variants per category, deterministic, no equal neighbours in a line.
- Staging: max 2 FX words per line, max 1 strong (configurable); the rest are light.
- Glow: per-category gain, builds toward word end, reduced on bright backdrops.
- Streak: geometry fn + cached alpha sprite, drawn on the overlay layer in `scene._fx_word`.
- Vocabulary stays in `assets/wordfx/*.json`.

## Out of scope
`marea/engine/*` (except removing 2 FX-only keys from `tuning.py`), `marea/audio/align.py`, line layout.
Bugs found there are only reported.

See `tasks/todo.md` for the task list and checkpoints.
