"""Mutable state of the engine core. Plain dataclasses, times in UTC epoch seconds, so the whole
state round-trips through JSON (:mod:`qost_engine.core.serial`) as the restart snapshot."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Span:
    """A state interval; ``end`` is ``None`` while it is the current state."""

    start: float
    end: float | None
    state: str
    reason: str | None = None


@dataclass
class Entity:
    """Current state of a unit or a line and its recent intervals."""

    code: str
    kind: str
    """``equipment`` | ``line``."""
    state: str | None = None
    since: float | None = None
    reason: str | None = None
    reason_source: str = "auto"
    alarm_code: str | None = None
    alarm: bool = False
    source: str = "engine"
    stop: str | None = None
    """Key of the open downtime record."""
    history: deque[Span] = field(default_factory=deque)


@dataclass
class Stop:
    """A downtime record (``DOWN_*`` / ``CHANGEOVER``) of a unit or a line."""

    entity: str
    line: str
    start: float
    end: float | None
    state: str
    reason: str
    reason_source: str
    planned: bool
    shift_date: str | None
    shift_code: str | None
    comment: str | None = None
    classified_ts: float | None = None
    flagged: bool = False
    """FR-ENG-04 "needs classification" already published."""
    alert: str | None = None
    """Dedup key of its AL-S1 alert."""

    @property
    def key(self) -> str:
        return f"{self.entity}|{self.start!r}"

    def duration(self, now: float) -> float:
        return (self.end if self.end is not None else now) - self.start


@dataclass
class LineAcc:
    """Counters of one line in one shift."""

    pq: int = 0
    gq: int = 0
    pri_produced_s: float = 0.0
    pri_good_s: float = 0.0
    out_to_next: int = 0
    """Bodies sent downstream (``pass`` + ``rework_pass``), for DQ-04."""


@dataclass
class ShiftAcc:
    """Accumulators of one working shift (open, or closed and still kept for recomputation)."""

    date: str
    code: str
    start: float
    end: float
    lines: dict[str, LineAcc] = field(default_factory=dict)
    buffer_start: dict[str, int] = field(default_factory=dict)
    partial: bool = False
    """Line states were unknown when the shift started (engine started mid-way)."""
    version: int = 0
    closed: bool = False
    area_rates: dict[str, float | None] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.date}/{self.code}"


@dataclass
class Point:
    ts: float
    level: int


@dataclass
class BufferSt:
    capacity: int
    level: int | None = None
    ts: float | None = None
    history: deque[Point] = field(default_factory=deque)


@dataclass
class Cond:
    """Debounced condition of a level-type rule (AL-B1, AL-L1)."""

    pending: str | None = None
    since: float | None = None
    active_key: str | None = None
    active_token: str | None = None
    clear_since: float | None = None


@dataclass
class OpenAlert:
    """An alert raised by the engine and not yet resolved by it."""

    key: str
    rule_id: str
    severity: str
    ts: float
    entity_type: str
    entity: str
    period_date: str
    shift: str | None
    period_key: str | None
    value: object
    escalation_level: int = 0
    escalation_due: float | None = None
    projection: bool = False


@dataclass
class SpcPoint:
    """One closed shift of an area on its p-chart (SPEC §11.3)."""

    key: str
    defects: int
    n: int
    special: bool = False


@dataclass
class CoreState:
    entities: dict[str, Entity] = field(default_factory=dict)
    stops: dict[str, Stop] = field(default_factory=dict)
    shifts: dict[str, ShiftAcc] = field(default_factory=dict)
    last_shift_end: float | None = None
    buffers: dict[str, BufferSt] = field(default_factory=dict)
    kits: dict[str, int] = field(default_factory=dict)
    alerts: dict[str, OpenAlert] = field(default_factory=dict)
    buffer_conds: dict[str, Cond] = field(default_factory=dict)
    ckd_conds: dict[str, Cond] = field(default_factory=dict)
    d1_minutes: dict[str, float] = field(default_factory=dict)
    """``equipment|local date`` -> closed unplanned minutes (AL-D1)."""
    bn_rates: dict[str, list[int]] = field(default_factory=dict)
    """Line -> PQ of the last closed shifts (FR-ENG-06 bottleneck rate)."""
    prev_area_rates: dict[str, float | None] = field(default_factory=dict)
    mtd_good: dict[str, int] = field(default_factory=dict)
    """``YYYY-MM`` -> good finished cars of closed shifts (last flow line)."""
    last_ts: dict[str, float] = field(default_factory=dict)
    """``entity|kind`` -> ts of the last applied event (late detection)."""
    watermark: float | None = None
    last_kpi_tick: float | None = None
    unknown_codes: dict[str, str] = field(default_factory=dict)
    late_events: int = 0
    dropped_units: int = 0
    events: int = 0
    health: dict[str, dict[str, float | None]] = field(default_factory=dict)
    """Unit -> latest PdM result ``{health_index, p_failure, ts}`` (M7b)."""
    pdm_last: float | None = None
    """Plant time of the last PdM tick."""
    spc: dict[str, list[SpcPoint]] = field(default_factory=dict)
    """Area -> recent closed shifts for the p-chart."""
