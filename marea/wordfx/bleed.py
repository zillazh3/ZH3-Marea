"""Spatial bleed kernel, cross-line bleed, and continuous line-handoff helpers."""
from typing import Sequence

from marea.wordfx.matcher import Match
from marea.wordfx.springs import eval_spring_240hz

BLEED_KERNEL: dict[int, float] = {1: 0.35, 2: 0.12}
CROSS_LINE_MAX_GAP_S = 2.5
MIN_HANDOFF_OVERLAP_S = 0.25
MAX_HANDOFF_OVERLAP_S = 0.40
DEFAULT_HANDOFF_OVERLAP_S = 0.32


def line_bleed_donors(
    line_idx: int,
    word_idx: int,
    line_matches: Sequence[Match],
    prev_line_last_hero: Match | None,
    line_start_ms: int,
) -> list[tuple[Match, float]]:
    """Return weighted bleed donors `[(donor_match, kernel_weight), ...]` for `(line_idx, word_idx)`.

    - Same-line neighbors: distance +-1 -> 0.35, +-2 -> 0.12.
    - Cross-line bleed: previous line's last hero word bleeds into words 0 (0.35) and 1 (0.12)
      when the vocal gap is continuous (`<= 2.5 s`).
    """
    out: list[tuple[Match, float]] = []
    for m in line_matches:
        if m.word == word_idx:
            continue
        dist = abs(m.word - word_idx)
        w = BLEED_KERNEL.get(dist, 0.0)
        if w > 0.0:
            out.append((m, w))
    if prev_line_last_hero is not None and word_idx in (0, 1):
        gap_s = max(0.0, (line_start_ms - prev_line_last_hero.end_ms) / 1000.0)
        if gap_s <= CROSS_LINE_MAX_GAP_S:
            w = BLEED_KERNEL[word_idx + 1]
            out.append((prev_line_last_hero, w))
    return out


def spring_bleed_arrival(t_s: float, donor_begin_s: float, donor_end_s: float, freq_hz: float = 2.8) -> float:
    """Spring-eased arrival envelope in [0, 1] so bleed glides in smoothly instead of snapping."""
    anti_s = donor_begin_s - 0.12
    if t_s <= anti_s:
        return 0.0
    tail_end = donor_end_s + 0.70

    def _target(x: float) -> float:
        if x < donor_begin_s:
            return 0.0
        if x <= donor_end_s:
            return 1.0
        if x <= tail_end:
            u = (x - donor_end_s) / max(0.05, tail_end - donor_end_s)
            return 1.0 - u * 0.75
        return 0.25

    val, _ = eval_spring_240hz(_target, t_s, anti_s - 0.05, freq_hz=freq_hz, zeta=1.0)
    return max(0.0, min(1.0, val))


def handoff_overlap_s(line_end_ms: int, next_line_start_ms: int | None) -> float:
    """Return outgoing release/fade overlap in [0.25, 0.40] s so continuous lines never go flat."""
    if next_line_start_ms is None:
        return DEFAULT_HANDOFF_OVERLAP_S
    gap_s = (next_line_start_ms - line_end_ms) / 1000.0
    if gap_s <= 0.15:
        return MAX_HANDOFF_OVERLAP_S
    if gap_s <= 1.50:
        return DEFAULT_HANDOFF_OVERLAP_S
    return MIN_HANDOFF_OVERLAP_S
