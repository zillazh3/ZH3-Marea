"""Repeated lyric rows on an unwrapped 60 Hz clock.

Rows retain their source objects and word timestamps. Only their scheduling times
move between cycles, so effect matching and syllable timing stay song-relative.
"""
import math
from contextlib import contextmanager

from marea.engine.engine import Engine
from marea.model.lyrics import Interlude, Lyrics


class LoopEngine(Engine):
    def __init__(self, lyrics, spec, measure, stage_h, font_px=42.0, params=None):
        super().__init__(Lyrics([]), measure, stage_h, font_px, params)
        self.spec = spec
        self.lyrics = lyrics
        selected = lyrics.lines[spec.first:spec.last + 1]
        entries = [(i, line) for i, line in enumerate(lyrics.lines) if spec.first <= i <= spec.last]
        for gap in lyrics.interludes:
            if selected[0].start <= gap.start and gap.end <= selected[-1].start:
                entries.append((len(lyrics.lines) + len(entries), gap))
        # Join both edge interludes into one dot sequence that can straddle the seam.
        first_start = selected[0].start
        last_end = max(line.end for line in selected)
        seam_end = first_start + spec.duration_ms
        edge_interlude = any(
            (gap.start < first_start and gap.end > spec.start_ms)
            or (gap.start < spec.end_ms and gap.end > last_end)
            for gap in lyrics.interludes
        )
        if seam_end > last_end and (edge_interlude or seam_end - last_end >= 2000):
            entries.append((len(lyrics.lines) + len(entries), Interlude(last_end, seam_end)))
        entries.sort(key=lambda entry: entry[1].start)
        self.preroll_ticks = 720
        # Extra cycles keep both clipped stage edges populated even for one-line loops.
        edge_cycles = max(3, math.ceil(stage_h * 2 / max(font_px, len(selected) * font_px)))
        before = math.ceil(self.preroll_ticks / spec.ticks) + edge_cycles
        for cycle in range(-before, edge_cycles + 2):
            offset = cycle * spec.duration_ms / 1000
            for stable_index, src in entries:
                item = self._build(stable_index, src)
                item.loop_cycle, item.loop_offset = cycle, offset
                item.start += offset
                item.end += offset
                self.items.append(item)
        for index, item in enumerate(self.items):
            item.index = index
            if item.is_gap and index + 1 < len(self.items):
                item.duet = self.items[index + 1].duet
        self.tick = -self.preroll_ticks - 1

    @contextmanager
    def source_clock(self, item):
        clock, start, end = self.t, item.start, item.end
        self.t = self.spec.start_ms / 1000 + (self.tick - item.loop_cycle * self.spec.ticks) / 60
        item.start, item.end = item.src.start / 1000, item.src.end / 1000
        try:
            yield
        finally:
            self.t, item.start, item.end = clock, start, end

    def _find_active(self):
        from marea.engine.engine import LOOKAHEAD

        return max((index for index, item in enumerate(self.items)
                    if item.start <= self.t + LOOKAHEAD + 1e-9), default=0)

    def _find_live(self, active):
        return frozenset([active] + [index for index in range(active)
                         if not self.items[index].is_gap and self.t < self.items[index].end - 1e-9
                         and self.items[index].end > self.items[active].start + 1e-9])

    def _render_item(self, item, on, dt):
        with self.source_clock(item):
            super()._render_item(item, on, dt)

    def _relayout(self, reason, now):
        super()._relayout(reason, now)
        # A stagger exactly on a frame boundary must fire on that frame in every cycle.
        for item in self.items:
            if item.y.pending is not None:
                target, deadline = item.y.pending
                deadline_tick = math.ceil(deadline * 60 / 1000 - 1e-9)
                item.y.pending = (target, deadline_tick * 1000 / 60)

    def clone(self):
        """Exact copy of the simulation state; source lines, effects, params and the measure callback stay shared."""
        memo = {id(self.measure): self.measure, id(self.lyrics): self.lyrics, id(self.spec): self.spec, id(self.p): self.p}
        for item in self.items:
            memo[id(item.src)] = item.src
            for word in item.words:
                memo[id(word.fx)] = word.fx
                memo[id(word.fx_layer)] = word.fx_layer
        return _dup(self, memo)

    def seek_tick(self, tick):
        while self.tick < tick:
            self.tick += 1
            # Compute from the integer lattice, avoiding cycle-dependent float drift.
            self.t = self.spec.start_ms / 1000 + self.tick / 60
            self.now = self.tick * 1000 / 60
            self._frame(1 / 60)


def _dup(obj, memo):
    """deepcopy for the engine's plain-attribute state objects (~6x faster than copy.deepcopy on ~7k objects)."""
    key = id(obj)
    if key in memo:
        return memo[key]
    kind = type(obj)
    if kind is list:
        out = memo[key] = []
        out.extend(_dup(v, memo) for v in obj)
        return out
    if kind is dict:
        out = memo[key] = {}
        out.update((k, _dup(v, memo)) for k, v in obj.items())
        return out
    if kind is tuple:
        items = tuple(_dup(v, memo) for v in obj)
        return obj if all(a is b for a, b in zip(items, obj)) else items
    if kind is set:
        return {_dup(v, memo) for v in obj}
    state = getattr(obj, "__dict__", None)
    if state is None or callable(obj):
        return obj  # numbers, strings, frozensets of ints, functions: immutable or shared by design
    out = memo[key] = kind.__new__(kind)
    out.__dict__.update((k, _dup(v, memo)) for k, v in state.items())
    return out
