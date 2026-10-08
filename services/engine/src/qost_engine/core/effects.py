"""Effects produced by the core: database rows to upsert and live messages to publish.

Each DB effect has a :attr:`key`; the writer coalesces effects with the same key (last wins), so
an interval opened and closed within one commit becomes a single row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class StateInterval:
    """Row of ``equipment_state`` (upsert by entity + start)."""

    entity: str
    entity_type: str
    start: datetime
    end: datetime | None
    state: str
    reason_code: str | None
    source: str

    @property
    def key(self) -> tuple[str, ...]:
        return ("equipment_state", self.entity, self.start.isoformat())


@dataclass(frozen=True, slots=True)
class DowntimeRow:
    """Row of ``downtime`` written by the engine (upsert by entity + start, import_id NULL)."""

    entity: str
    line: str
    start: datetime
    end: datetime | None
    duration_s: float | None
    planned: bool
    microstop: bool
    reason_code: str
    reason_source: str
    shift_date: date | None
    shift_code: str | None
    comment: str | None = None
    classified_ts: datetime | None = None

    @property
    def key(self) -> tuple[str, ...]:
        return ("downtime", self.entity, self.start.isoformat())


@dataclass(frozen=True, slots=True)
class KpiShiftRow:
    """New row of ``kpi_shift`` (``source=events``; a new ``version`` per recomputation)."""

    line: str
    shift_date: date
    shift_code: str
    version: int
    final: bool
    values: dict[str, Any]
    computed_ts: datetime

    @property
    def key(self) -> tuple[str, ...]:
        return (
            "kpi_shift",
            self.line,
            self.shift_date.isoformat(),
            self.shift_code,
            str(self.version),
        )


@dataclass(frozen=True, slots=True)
class BottleneckRow:
    line_group: str
    shift_date: date
    shift_code: str
    line: str
    sole_share: float
    shifting_share: float

    @property
    def key(self) -> tuple[str, ...]:
        return (
            "bottleneck_shift",
            self.line_group,
            self.shift_date.isoformat(),
            self.shift_code,
            self.line,
        )


@dataclass(frozen=True, slots=True)
class AlertUpsert:
    """Upsert of ``alert`` by dedup key. An existing ``ack`` status is kept unless resolved."""

    dedup_key: str
    ts: datetime
    rule_id: str
    severity: str
    entity_type: str
    entity: str
    title_ru: str
    message_ru: str
    value: Any
    status: str
    resolved_ts: datetime | None
    recipients: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, ...]:
        return ("alert", self.dedup_key)

    def message(self) -> dict[str, Any]:
        return {
            "dedup_key": self.dedup_key,
            "ts": self.ts.isoformat(),
            "rule_id": self.rule_id,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity": self.entity,
            "title_ru": self.title_ru,
            "message_ru": self.message_ru,
            "value": self.value,
            "status": self.status,
            "resolved_ts": self.resolved_ts.isoformat() if self.resolved_ts else None,
            "recipients": list(self.recipients),
            "channels": list(self.channels),
        }


@dataclass(frozen=True, slots=True)
class AlertEscalate:
    """Raise ``escalation_level`` of a still open (not acknowledged) alert."""

    dedup_key: str
    level: int
    roles: tuple[str, ...]
    ts: datetime

    @property
    def key(self) -> tuple[str, ...]:
        return ("alert_escalate", self.dedup_key, str(self.level))


@dataclass(frozen=True, slots=True)
class DqUpsert:
    """Upsert of ``dq_issue`` by dedup key."""

    dedup_key: str
    ts: datetime
    rule_id: str
    severity: str
    entity: str
    period_date: date | None
    details: dict[str, Any]

    @property
    def key(self) -> tuple[str, ...]:
        return ("dq_issue", self.dedup_key)


@dataclass(frozen=True, slots=True)
class AuditRow:
    ts: datetime
    action: str
    entity_type: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    user: str | None = None

    @property
    def key(self) -> tuple[str, ...]:
        return ("audit_log", self.action, self.entity_id, self.ts.isoformat())


@dataclass(frozen=True, slots=True)
class ReclassifyRequest:
    """Reclassification of a stop the core no longer holds (older than the retained shifts):
    the writer updates the stored rows and recomputes the affected shifts (FR-ENG-03, KPI-04)."""

    entity: str
    start: datetime
    reason_code: str
    planned: bool
    comment: str | None
    user: str | None
    ts: datetime

    @property
    def key(self) -> tuple[str, ...]:
        return ("reclassify", self.entity, self.start.isoformat(), self.ts.isoformat())


@dataclass(frozen=True, slots=True)
class PredictionRow:
    """Row of ``prediction`` (upsert by equipment + horizon + ts): PdM serving, SPEC §11.1."""

    equipment: str
    ts: datetime
    horizon_h: float
    p_failure: float
    health_index: float
    model_version: str
    top_factors: list[dict[str, Any]]

    @property
    def key(self) -> tuple[str, ...]:
        return ("prediction", self.equipment, self.ts.isoformat(), repr(self.horizon_h))


Effect = (
    StateInterval
    | DowntimeRow
    | KpiShiftRow
    | BottleneckRow
    | AlertUpsert
    | AlertEscalate
    | DqUpsert
    | AuditRow
    | ReclassifyRequest
    | PredictionRow
)


@dataclass(frozen=True, slots=True)
class LiveMsg:
    """A live delta (``live`` channel, WS envelope) and the snapshot entry it updates.

    ``store`` = (``name``, ``field``, ``value``): ``HSET live:{name} field value`` when ``field`` is
    set, ``SET live:{name} value`` otherwise; a ``None`` value deletes the entry; ``store=None``
    publishes only.
    """

    type: str
    ts: datetime
    data: dict[str, Any]
    store: tuple[str, str | None, dict[str, Any] | None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
