"""Bottleneck detection (SPEC §9.5).

* Aggregate path (imported periods, no state timelines): the bottleneck of a day is the flow step
  with the smallest output; over the period, the one with the smallest mean output; ``shifting``
  when the daily bottleneck changed (:func:`aggregate_bottleneck`).
* Active-period method for event data (Roser, Nakano, Tanaka, "Shifting Bottleneck Detection",
  WSC 2002): :func:`active_periods` turns a line's state timeline into maximal active periods;
  :func:`shifting_bottleneck` finds b(t), the sole and shifting bottleneck time per line and the
  bottleneck of the window.

Time is a plain number (seconds or minutes, any origin) so the method is unit-agnostic; callers
convert datetimes (e.g. seconds since the window start).

Overlapping shifting regions (two consecutive bottleneck changes whose period intersections
overlap) are not defined by SPEC §9.5; each moment of such an overlap is attributed to the change
nearest in time, so the invariant "at every moment either one line is the sole bottleneck, or
exactly two are shifting, or there is none" always holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import Any

from twin_core.domain import EquipmentState

INACTIVE_STATES: frozenset[str] = frozenset(
    {EquipmentState.STARVED, EquipmentState.BLOCKED, EquipmentState.IDLE_NO_PLAN}
)
"""A line is active in every other state (a change between active states keeps the period)."""


@dataclass(frozen=True, slots=True)
class AggregateBottleneck:
    by_day: Mapping[date, str]
    overall: str | None
    shifting: bool
    mean_output: Mapping[str, float]
    """Mean output per period unit (shift) of each flow step."""

    def to_report(self) -> dict[str, Any]:
        """The import-report form (``import_expected.json: bottleneck_aggregate``)."""
        return {
            "by_day": {day.isoformat(): step for day, step in sorted(self.by_day.items())},
            "overall": self.overall,
            "shifting": self.shifting,
        }


def aggregate_bottleneck(
    output: Mapping[date, Mapping[str, float]], order: Sequence[str]
) -> AggregateBottleneck:
    """Bottleneck by minimum output.

    Args:
        output: ``day -> step -> output`` (step = area or line code).
        order: flow order of the steps; ties go to the earliest step in this order.
    """
    rank = {step: i for i, step in enumerate(order)}

    def first_min(values: Mapping[str, float]) -> str | None:
        steps = sorted((s for s in values if s in rank), key=rank.__getitem__)
        return min(steps, key=values.__getitem__) if steps else None

    by_day: dict[date, str] = {}
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for day in sorted(output):
        step = first_min(output[day])
        if step is not None:
            by_day[day] = step
        for name, value in output[day].items():
            if name in rank:
                totals[name] = totals.get(name, 0.0) + value
                counts[name] = counts.get(name, 0) + 1
    mean = {step: totals[step] / counts[step] for step in order if step in totals}
    return AggregateBottleneck(
        by_day=by_day,
        overall=first_min(mean),
        shifting=len(set(by_day.values())) > 1,
        mean_output=mean,
    )


# =========================================================================== active periods


def is_active(state: str) -> bool:
    """Activity of a line for the active-period method (SPEC §9.5, step 1)."""
    return state not in INACTIVE_STATES


@dataclass(frozen=True, slots=True)
class Period:
    """A maximal active period ``[start, end)`` (clipped to the window).

    ``full_length`` is the length before clipping; it only breaks ties between equally long
    clipped periods (two lines active through the whole window), before the flow order.
    """

    start: float
    end: float
    full_length: float | None = None

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def rank_length(self) -> tuple[float, float]:
        full = self.full_length if self.full_length is not None else self.length
        return (self.length, full)


def active_periods(
    timeline: Iterable[tuple[float, float, str]], window: tuple[float, float]
) -> list[Period]:
    """Maximal active periods of one line, clipped to ``window``.

    ``timeline`` holds ``(start, end, state)`` intervals in time order; touching active intervals
    merge into one period, a gap (no data) or an inactive state ends it. Clipping happens after
    merging, as SPEC §9.5 prescribes ("активные периоды обрезаются границами окна").
    """
    merged: list[list[float]] = []
    for start, end, state in timeline:
        if end <= start or not is_active(state):
            continue
        if merged and merged[-1][1] >= start:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    lo, hi = window
    out: list[Period] = []
    for start, end in merged:
        s, e = max(start, lo), min(end, hi)
        if e > s:
            out.append(Period(s, e, end - start))
    return out


@dataclass(frozen=True, slots=True)
class Segment:
    """``[start, end)`` with constant bottleneck ``line`` (``None``: no line active)."""

    start: float
    end: float
    line: str | None


@dataclass(frozen=True, slots=True)
class Transition:
    """b(t) changes from ``src`` to ``dst`` at ``at``; overlap of their periods = shifting."""

    at: float
    src: str
    dst: str
    shift_start: float
    shift_end: float


@dataclass(frozen=True, slots=True)
class Piece:
    """Elementary time piece with its classification (for invariants and timelines)."""

    start: float
    end: float
    sole: str | None
    shifting: tuple[str, str] | None


@dataclass(frozen=True, slots=True)
class ShiftingBottleneck:
    """Result of the active-period method over a window."""

    window: tuple[float, float]
    segments: tuple[Segment, ...]
    transitions: tuple[Transition, ...]
    pieces: tuple[Piece, ...]
    sole: Mapping[str, float] = field(default_factory=dict)
    shifting: Mapping[str, float] = field(default_factory=dict)
    overall: str | None = None

    @property
    def length(self) -> float:
        return self.window[1] - self.window[0]

    def share(self, value: float) -> float:
        return value / self.length if self.length > 0 else 0.0

    @property
    def sole_share(self) -> dict[str, float]:
        return {line: self.share(v) for line, v in self.sole.items()}

    @property
    def shifting_share(self) -> dict[str, float]:
        return {line: self.share(v) for line, v in self.shifting.items()}

    @property
    def current(self) -> str | None:
        """Bottleneck at the end of the window (live: "now")."""
        return self.segments[-1].line if self.segments else None

    @property
    def current_since(self) -> float | None:
        """Start of the current bottleneck's uninterrupted run."""
        if not self.segments or self.segments[-1].line is None:
            return None
        return self.segments[-1].start

    def shares(self) -> dict[str, dict[str, float]]:
        """``line -> {"sole": share, "shifting": share}`` for every line with any share."""
        lines = sorted(set(self.sole) | set(self.shifting))
        return {
            line: {
                "sole": self.share(self.sole.get(line, 0.0)),
                "shifting": self.share(self.shifting.get(line, 0.0)),
            }
            for line in lines
        }


def _covering(periods: Sequence[Period], a: float, b: float) -> Period | None:
    for p in periods:
        if p.start <= a and p.end >= b:
            return p
    return None


def shifting_bottleneck(
    periods: Mapping[str, Sequence[Period]],
    window: tuple[float, float],
    order: Sequence[str] | None = None,
) -> ShiftingBottleneck:
    """Active-period bottleneck detection over ``window`` (SPEC §9.5, steps 2–4).

    ``periods`` are the (clipped) active periods per line — for a live window ending "now", open
    periods end at now, so their elapsed part counts. Equal clipped lengths are decided by the
    unclipped length (:attr:`Period.full_length`), then by flow order (``order``); ties of the
    window's bottleneck go to flow order too.
    """
    lo, hi = window
    rank = {line: i for i, line in enumerate(order or sorted(periods))}
    lines = sorted(periods, key=lambda line: (rank.get(line, len(rank)), line))
    if hi <= lo:
        return ShiftingBottleneck(window, (), (), ())
    points = {lo, hi}
    for line in lines:
        for p in periods[line]:
            points.update(x for x in (p.start, p.end) if lo < x < hi)
    grid = sorted(points)

    # step 2: b(t) on elementary segments, then merged runs
    owners: list[tuple[float, float, str | None, Period | None]] = []
    for a, b in pairwise(grid):
        best: tuple[str, Period] | None = None
        for name in lines:
            cover = _covering(periods[name], a, b)
            if cover is not None and (best is None or cover.rank_length > best[1].rank_length):
                best = (name, cover)
        owners.append((a, b, best[0] if best else None, best[1] if best else None))
    segments: list[Segment] = []
    for a, b, owner_line, _p in owners:
        if segments and segments[-1].line == owner_line and segments[-1].end == a:
            segments[-1] = Segment(segments[-1].start, b, owner_line)
        else:
            segments.append(Segment(a, b, owner_line))

    # step 3: transitions X -> Y and their shifting intervals
    transitions: list[Transition] = []
    for prev, nxt in pairwise(owners):
        src, dst = prev[2], nxt[2]
        if src is None or dst is None or src == dst:
            continue
        at = prev[1]
        px = _covering(periods[src], prev[0], at)
        py = _covering(periods[dst], at, nxt[1])
        if px is None or py is None:  # pragma: no cover - owners imply coverage
            continue
        s, e = max(px.start, py.start), min(px.end, py.end)
        transitions.append(Transition(at, src, dst, s, max(s, e)))

    # step 3b: classify pieces; overlaps go to the nearest change
    cuts = set(grid)
    for t in transitions:
        cuts.update(x for x in (t.shift_start, t.shift_end, t.at) if lo < x < hi)
    for t1, t2 in pairwise(transitions):
        mid = (t1.at + t2.at) / 2
        if lo < mid < hi:
            cuts.add(mid)
    fine = sorted(cuts)
    sole: dict[str, float] = {}
    shifting: dict[str, float] = {}
    pieces: list[Piece] = []
    seg_i = 0
    for a, b in pairwise(fine):
        while segments[seg_i].end <= a:
            seg_i += 1
        owner = segments[seg_i].line
        mid = (a + b) / 2
        candidates = [t for t in transitions if t.shift_start <= a and t.shift_end >= b]
        if candidates:
            t = min(candidates, key=lambda c: (abs(c.at - mid), c.at))
            for line in (t.src, t.dst):
                shifting[line] = shifting.get(line, 0.0) + (b - a)
            pieces.append(Piece(a, b, None, (t.src, t.dst)))
        elif owner is not None:
            sole[owner] = sole.get(owner, 0.0) + (b - a)
            pieces.append(Piece(a, b, owner, None))
        else:
            pieces.append(Piece(a, b, None, None))

    # step 4: bottleneck of the window
    totals = {line: sole.get(line, 0.0) + shifting.get(line, 0.0) for line in lines}
    overall = None
    best_total = 0.0
    for line in lines:
        if totals[line] > best_total:
            overall, best_total = line, totals[line]
    return ShiftingBottleneck(
        window, tuple(segments), tuple(transitions), tuple(pieces), sole, shifting, overall
    )
