"""Timing shared by seamless export, preview, and audio."""
import math
from dataclasses import dataclass
from typing import Sequence

HZ = 60
AUDIO_FADE_MS = 150
VIDEO_FADE_MS = 500
AUDIO_FADE_RANGE = (25, 300)
VIDEO_FADE_RANGE = (100, 1000)
BAR_NUDGE_MS = 250


@dataclass(frozen=True)
class LoopSpec:
    start_ms: float
    first: int
    last: int
    ticks: int
    audio_fade_ms: float = AUDIO_FADE_MS
    video_fade_ms: float = VIDEO_FADE_MS

    @property
    def duration_ms(self) -> float:
        return self.ticks * 1000 / HZ

    @property
    def end_ms(self) -> float:
        return self.start_ms + self.duration_ms

    @classmethod
    def for_range(cls, start_ms: float, end_ms: float, first: int, last: int, fps: int = HZ,
                  audio_fade_ms: float = AUDIO_FADE_MS, video_fade_ms: float = VIDEO_FADE_MS) -> "LoopSpec":
        if fps not in (30, HZ):
            raise ValueError("Seamless clips require 30 or 60 fps")
        if not math.isfinite(start_ms) or not math.isfinite(end_ms) or end_ms < start_ms or not 0 <= first <= last:
            raise ValueError("Invalid seamless clip range")
        frames = max(1, math.ceil((end_ms - start_ms) * fps / 1000 - 1e-9))
        return cls(start_ms, first, last, frames * (HZ // fps), audio_fade_ms, video_fade_ms)


def suggest_bar_end(start_ms: float, lyrics_end_ms: float, end_ms: float, beats_ms: Sequence[float],
                    beat_period_ms: float, offset_ms: float, audio_duration_ms: float) -> float | None:
    """Keep the start's beat phase and propose a nearby end after the final syllable."""
    if len(beats_ms) < 5 or not math.isfinite(beat_period_ms) or beat_period_ms <= 0:
        return None
    source_start = start_ms + offset_ms
    first_beat = min(beats_ms, key=lambda beat: abs(beat - source_start))
    bar_ms = 4 * beat_period_ms
    count = max(1, round((end_ms - start_ms) / bar_ms))
    expected = first_beat + count * bar_ms
    next_beat = min(beats_ms, key=lambda beat: abs(beat - expected))
    if abs(next_beat - expected) > beat_period_ms * 0.35:
        return None
    proposed = start_ms + next_beat - first_beat
    if lyrics_end_ms <= proposed <= audio_duration_ms - offset_ms and 1 <= abs(proposed - end_ms) <= BAR_NUDGE_MS:
        return proposed
    return None
