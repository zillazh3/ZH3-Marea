# Independent vocal performances

The experimental review window aligns every imported performance, including
backing vocals, rather than aligning only the lead. Each performance retains its
own text, line interval, syllables, agent, role, and source stem. The same agent
can occupy several overlapping lanes. No timing boundary is clipped to another
performance or to its TTML parent.

## Local detection

Enable **Separate lead/backing vocals** explicitly in the experimental review
window. It is off by default. Supply the following assets separately in the
configured laboratory's `separator-models` directory, with their matching model
configuration/catalog assets required by audio-separator:

- `model_bs_roformer_ep_317_sdr_12.9755.ckpt`: isolate all vocals.
- `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`: produce karaoke/backing
  and residual/lead outputs from the vocal stem.

The optional pinned audio-separator dependency is reused. No dependency, weight,
or audio upload service is added. Processing runs in the existing sidecar with
network connections disabled; download-mode workers remain separate.
Model output labels determine routing; file order and filename guesses do not.
Missing models, ambiguous labels, or mismatched stem durations are errors.

Whisper transcribes the lead and backing outputs independently. Acoustic energy,
envelope, and pitch comparisons suggest roles. These scores are uncalibrated,
and the UI requires review of new secondary candidates. Suspected separator
leakage remains an uncertain candidate. A detected role never establishes a
singer's identity. Unknown agents remain unknown.

Without lead/backing separation, acoustic overlap regions are shown without
inventing secondary text. Add or split performances manually when needed. Two
stems cannot recover an arbitrary number of simultaneous singers, and a shared
stem cannot provide reliable independent sustain boundaries. Such results remain
approximate and require correction. Timing precision and detection accuracy on
real music must be evaluated separately; synthetic tests do not establish them.

## Editing and review

The existing waveform, audition, zoom, timing controls, undo, and section
recalculation are retained. Lane groups identify role, agent, and overlapping
take, with vertical scrolling for additional performances.

- Correct a selected word or syllable without editing another performance.
- Replacing complete line text explicitly resets its segmentation to an
  approximate interval; recalculation supplies fresh words/syllables.
- Apply a role or agent without changing the audio stem used for recalculation.
- Accept or reject each pending secondary candidate before exporting. Acceptance
  does not pin timing. Accepted uncertain roles and approximate timings are valid.
- Save unfinished work with **Save review JSON**, including pending candidates,
  rejected records, and unresolved overlap regions.

Imported line-only TTML remains line-only until explicitly aligned. Original
syllables remain authoritative unless explicitly corrected. Unsupported nested
inline edits or edits that would discard span attributes are refused at editing
or export. The original source file is never rewritten.

## Persistence and compatibility

Alignment JSON uses schema `marea.alignment`, version `2`. Version `1` remains
readable; missing review state defaults to accepted. New per-line fields are
`source_stem`, `review_state`, and `role_reviewed`. Rejected performances are
retained in `source_lyrics.rejected`; possible overlap regions are retained in
`source_lyrics.overlaps`. Cached alignment and ASR results are versioned separately
from cached audio decoding and emissions, so the latter can still be reused.

TTML retains independent `<p>` elements and independently timed
`<span ttm:role="x-bg">` elements, with timed child spans per existing syllable.
Role metadata uses the existing Marea namespace. `marea:agent-unknown` records an
unknown agent, and optional `marea:begin` / `marea:end` retain a lead-only envelope
when its enclosing paragraph also covers backing vocals. Container envelopes may
grow to contain children without changing either performance's timing.

The common offset is applied once during TTML export. Missing imported alignment
results keep reference boundaries, converted to the common audio clock and marked
approximate. Per-performance text, raw agent, role, line intervals, and syllable
intervals are checked by export/reimport. Output remains collision-safe
`*.aligned.ttml` with adjacent `*.alignment.json`.

Implementation reuses the model/parser, alignment request helpers, session,
TTML serializer, sidecar stages, CTC/syllabification code, Qt runner, and timeline.
The affected surfaces are `marea/lab`, `marea/ui/lab.py`, and the sidecar layer,
pipeline, stage, and worker modules. Regression tests are in
`tests/test_vocal_layers.py`, `tests/test_vocal_worker.py`, and
`tests/test_vocal_ui.py`, alongside the existing lab tests.

Files changed for this feature:

- `marea/lab/alignment.py`, `align_job.py`, `session.py`, `ttml_in.py`, `ttml_out.py`.
- `marea/ui/lab.py`.
- `sidecar/layers.py`, `pipeline.py`, `stages.py`, `worker.py`.
- `tests/test_lab_ttml.py`, `test_lab_session.py`, `test_lab_ui.py`.
- New: `tests/test_vocal_layers.py`, `test_vocal_worker.py`, `test_vocal_ui.py`.
- `THIRD_PARTY.md` and this new documentation file.

Existing test expectations changed only for the version-2 contract, preservation
of draft timing/provenance, explicit imported-text correction, and line-only
TTML preservation. Reference/golden fixtures were not edited.

## Licenses and validation

[audio-separator code is MIT licensed](https://github.com/nomadkaraoke/python-audio-separator/blob/v0.47.1/LICENSE).
That license does not establish permission for every model's weights. The
[karaoke-weight licensing question remains open](https://github.com/Anjok07/ultimatevocalremovergui/issues/2295).
Neither BS-Roformer nor karaoke weights are bundled or redistributed. Supplying
weights locally does not resolve their license; verify permissions before use
or distribution.

Use the project `.venv` for focused tests. On restricted Windows hosts, direct
test temporary directories into `out` and use the null test keyring to avoid
accessing real credentials. Process-tree termination may require an unrestricted
test process. Real-audio WER, timing error, false positives/negatives, elapsed
time, RAM, and peak VRAM remain separate opt-in evaluation work.
