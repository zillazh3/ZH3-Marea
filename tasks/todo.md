# Word FX v2 - task list

Test command: `.venv/Scripts/python -m pytest tests/test_wordfx*.py tests/test_glow.py tests/test_preview_debug.py`
Known baseline failures: ffmpeg export tests, `tests/test_vocal_ui.py`.

## Phase 0
- [x] T0 Timing task committed (9e14e1e, 4503013, 77e36c2); baseline `tests/test_wordfx*.py tests/test_glow.py tests/test_preview_debug.py`: 577 passed (84 s, QT_QPA_PLATFORM=offscreen)

## Phase 1 - Foundation
- [x] T1 `config.py` + migrate scattered constants (values unchanged, tests/goldens unchanged)
- [x] T2 Identical choruses: seed from line text + word index; remove repeat escalation; update tests + docs
- [x] T3 Variant framework (`variants.py`, `FxParams.variant`, song override, debug label); no equal neighbours
- [x] Checkpoint A: 596 wordfx/glow/tuning tests pass (user asked to continue through all phases without stopping for review)

## Phase 2 - Motion, staging, glow, streak
- [x] T4 Lifecycle + 12 principles (eased attack, anticipation dip, stretch 2-4%, follow-through, arcs; exact return to base)
- [x] T5 Staging + density (strong/light tiers, particle budget)
- [x] T6 Glow rework (per-category gain, build curve, white core, bright-background reduction)
- [x] T7 Micro light streak (geometry, cached sprite, resolution-scaled thickness, per-category strength)
- [x] Checkpoint B: automated tests pass; visual sheet + frame_ms pending (see below)

## Phase 3 - Variety
- [x] T8 New primitives (shimmer sweep, scatter, frost, ink bleed, soft glitch, underline ripple, heartbeat, attack shake, dim core, heat shimmer)
- [x] T9 Refine the 12 spec categories with 2-3 variants each; fix inert particle kinds
- [x] T10 FX for new categories (hope, loneliness, desire, freedom, light, electric, cold, death, scream, whisper, breath)
- [x] Checkpoint C: category x variant render test (scene) + contact sheets

## Phase 4 - Classification (delegated, parallel with Phase 3)
- [x] T11 New categories in `library.json`/`rules.json`; fix `solo/sola`, `sol`, `rose`, `rosa`, `seas`, `live`, `burning/burnt`; tests

## Phase 5 - Tooling and docs
- [x] T12 FX Gallery (window, menu entry, live sliders via `set_fx`, copy-config button, smoke test)
- [x] T13 (gallery paragraph assumes T12 lands as specified) Update `docs/wordfx.md`; final summary
- [x] Checkpoint D: 744 wordfx+gallery, 62 editor/lab, 59 engine/render tests pass; same frame identical across scenes

## FINAL STEP (requested by user)
- [ ] After ALL phases are done and verified: run `shutdown /s` (user asked for it explicitly). Report results first.
