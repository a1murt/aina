"""Unified event schema (SPEC §7.2): one envelope for every source (sim, OPC UA, MQTT, import, ...).

Every event is an immutable pydantic model discriminated by ``kind``::

    {"event_id": "01JA7Q3M4X2B7Z9K0F6T8R5N1C", "ts": "2026-10-16T04:31:12.123000Z",
     "received_ts": "2026-10-16T04:31:12.480000Z", "source": "opcua", "site": "KST",
     "entity_type": "equipment", "entity": "CONV-03", "kind": "state",
     "data": {"state": "DOWN_UNPLANNED", "reason_code": "ME-CHAIN", "alarm_code": "ME-CHAIN"},
     "quality": "good"}

``event_id`` is a ULID and the idempotency key (writers use ``ON CONFLICT DO NOTHING``).
Timestamps are aware UTC and serialize with a ``Z`` suffix and microseconds.

Unit-result contract (ISO 22400 counters, SPEC §5.4/§5.5): ``pass``, ``defect`` and ``scrap`` mark
the *first* exit of a body from a line (counted in PQ; ``pass`` also in GQ); ``rework_pass`` is a
repeated exit after rework or repaint and is never counted in PQ again.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from twin_core.clock import ensure_utc, format_utc
from twin_core.config.common import Code, Ident, NonEmptyStr
from twin_core.domain import Disposition, EquipmentState

EventKind = Literal[
    "state", "unit", "defect", "telemetry", "alarm", "buffer_level", "operator", "ckd"
]
EventSource = Literal["opcua", "mqtt", "import", "operator", "sim", "engine"]
EntityType = Literal["site", "area", "line", "equipment", "buffer", "product"]
Quality = Literal["good", "uncertain", "bad"]
UnitResult = Literal["pass", "defect", "rework_pass", "scrap"]
OperatorAction = Literal["andon", "classify_downtime", "log_defect", "material_call"]
CkdEventType = Literal["consume", "delivery", "set"]

FIRST_EXIT_RESULTS: frozenset[str] = frozenset({"pass", "defect", "scrap"})
"""Unit results that count in PQ (first exit of a body from a line)."""
FINISHED_RESULTS: frozenset[str] = frozenset({"pass", "rework_pass"})
"""Unit results at the last flow line that make a finished car (scrap never does)."""

# --------------------------------------------------------------------------- ULID

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {ch: i for i, ch in enumerate(_CROCKFORD)}
ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_MAX_MS = (1 << 48) - 1

Ulid = Annotated[str, StringConstraints(pattern=ULID_PATTERN)]


def encode_ulid(ms: int, randomness: bytes) -> str:
    """ULID string from a 48-bit millisecond timestamp and 80 bits (10 bytes) of randomness."""
    if not 0 <= ms <= _MAX_MS:
        raise ValueError(f"ULID timestamp out of range: {ms}")
    if len(randomness) != 10:
        raise ValueError(f"ULID randomness must be 10 bytes, got {len(randomness)}")
    value = (ms << 80) | int.from_bytes(randomness, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid_ms(ts: datetime) -> int:
    """Milliseconds since the Unix epoch of an aware datetime (the ULID time component)."""
    delta = ensure_utc(ts) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def ulid_timestamp(ulid: str) -> datetime:
    """Decode the time component of a ULID (millisecond precision)."""
    value = 0
    for ch in ulid.upper():
        if ch not in _DECODE:
            raise ValueError(f"invalid ULID character {ch!r} in {ulid!r}")
        value = (value << 5) | _DECODE[ch]
    ms = value >> 80
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class EventIds(Protocol):
    """Factory of event ids: one new ULID per call, time component taken from ``ts``."""

    def __call__(self, ts: datetime) -> str: ...


class RandomIds:
    """ULIDs with OS randomness (collector, api)."""

    def __call__(self, ts: datetime) -> str:
        return encode_ulid(ulid_ms(ts), os.urandom(10))


class DeterministicIds:
    """Reproducible ULIDs: randomness = blake2b(seed | nonce | sequence number).

    The simulator uses it so that the same seed, interventions and nonce give bit-identical
    events (FR-SIM-01) and re-running a backfill is idempotent.
    """

    def __init__(self, seed: int | str, nonce: str) -> None:
        self._prefix = f"{seed}|{nonce}|".encode()
        self._seq = 0

    @property
    def issued(self) -> int:
        return self._seq

    def __call__(self, ts: datetime) -> str:
        digest = hashlib.blake2b(self._prefix + str(self._seq).encode(), digest_size=10).digest()
        self._seq += 1
        return encode_ulid(ulid_ms(ts), digest)


# --------------------------------------------------------------------------- payloads


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StateData(_Data):
    state: EquipmentState
    reason_code: Code | None = None
    alarm_code: NonEmptyStr | None = None


class UnitData(_Data):
    line: Code
    body_id: NonEmptyStr
    product: Code
    result: UnitResult
    defect_code: Code | None = None


class DefectData(_Data):
    line: Code
    defect_code: Code
    qty: Annotated[int, Field(gt=0)]
    body_id: NonEmptyStr | None = None
    disposition: Disposition


class TelemetryData(_Data):
    signal: Ident
    value: float
    unit: str


class AlarmData(_Data):
    code: NonEmptyStr
    active: bool
    text: str = ""


class BufferLevelData(_Data):
    buffer: Code
    level: Annotated[int, Field(ge=0)]
    capacity: Annotated[int, Field(gt=0)]


class OperatorData(_Data):
    action: OperatorAction
    user: NonEmptyStr
    payload: dict[str, JsonValue] = {}


class CkdData(_Data):
    product: Code
    kits: Annotated[int, Field(ge=0)]
    event: CkdEventType


# --------------------------------------------------------------------------- envelope


class EventBase(BaseModel):
    """Fields common to every event kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: Ulid
    ts: AwareDatetime
    received_ts: AwareDatetime | None = None
    source: EventSource
    site: Code
    entity_type: EntityType
    entity: Code
    quality: Quality = "good"

    @field_validator("ts", "received_ts")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("ts", "received_ts")
    def _ser_ts(self, value: datetime | None) -> str | None:
        return None if value is None else format_utc(value)


class StateEvent(EventBase):
    kind: Literal["state"] = "state"
    data: StateData


class UnitEvent(EventBase):
    kind: Literal["unit"] = "unit"
    data: UnitData


class DefectEvent(EventBase):
    kind: Literal["defect"] = "defect"
    data: DefectData


class TelemetryEvent(EventBase):
    kind: Literal["telemetry"] = "telemetry"
    data: TelemetryData


class AlarmEvent(EventBase):
    kind: Literal["alarm"] = "alarm"
    data: AlarmData


class BufferLevelEvent(EventBase):
    kind: Literal["buffer_level"] = "buffer_level"
    data: BufferLevelData


class OperatorEvent(EventBase):
    kind: Literal["operator"] = "operator"
    data: OperatorData


class CkdEvent(EventBase):
    kind: Literal["ckd"] = "ckd"
    data: CkdData


Event = Annotated[
    StateEvent
    | UnitEvent
    | DefectEvent
    | TelemetryEvent
    | AlarmEvent
    | BufferLevelEvent
    | OperatorEvent
    | CkdEvent,
    Field(discriminator="kind"),
]
"""Any event (discriminated by ``kind``)."""

AnyEvent = (
    StateEvent
    | UnitEvent
    | DefectEvent
    | TelemetryEvent
    | AlarmEvent
    | BufferLevelEvent
    | OperatorEvent
    | CkdEvent
)

EVENT_CLASSES: Mapping[str, type[EventBase]] = {
    "state": StateEvent,
    "unit": UnitEvent,
    "defect": DefectEvent,
    "telemetry": TelemetryEvent,
    "alarm": AlarmEvent,
    "buffer_level": BufferLevelEvent,
    "operator": OperatorEvent,
    "ckd": CkdEvent,
}

_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(Event)


def parse_event(raw: str | bytes | Mapping[str, Any]) -> AnyEvent:
    """Validate a JSON document or a mapping into a typed event."""
    if isinstance(raw, str | bytes):
        return _ADAPTER.validate_json(raw)
    return _ADAPTER.validate_python(dict(raw))


def make_event(
    kind: EventKind,
    *,
    event_id: str,
    ts: datetime,
    source: EventSource,
    site: str,
    entity_type: EntityType,
    entity: str,
    data: Mapping[str, Any],
    received_ts: datetime | None = None,
    quality: Quality = "good",
) -> AnyEvent:
    """Build and validate an event of ``kind``."""
    return parse_event(
        {
            "event_id": event_id,
            "ts": ts,
            "received_ts": received_ts,
            "source": source,
            "site": site,
            "entity_type": entity_type,
            "entity": entity,
            "kind": kind,
            "data": dict(data),
            "quality": quality,
        }
    )


def dumps(event: EventBase) -> str:
    """Compact JSON of one event (stable key order: envelope, then ``data``)."""
    return event.model_dump_json(exclude_none=False)


def to_dict(event: EventBase) -> dict[str, Any]:
    """JSON-compatible dict of an event."""
    result: dict[str, Any] = json.loads(dumps(event))
    return result


__all__ = [
    "EVENT_CLASSES",
    "FINISHED_RESULTS",
    "FIRST_EXIT_RESULTS",
    "ULID_PATTERN",
    "AlarmData",
    "AlarmEvent",
    "AnyEvent",
    "BufferLevelData",
    "BufferLevelEvent",
    "CkdData",
    "CkdEvent",
    "DefectData",
    "DefectEvent",
    "DeterministicIds",
    "EntityType",
    "Event",
    "EventBase",
    "EventIds",
    "EventKind",
    "EventSource",
    "OperatorData",
    "OperatorEvent",
    "Quality",
    "RandomIds",
    "StateData",
    "StateEvent",
    "TelemetryData",
    "TelemetryEvent",
    "UnitData",
    "UnitEvent",
    "UnitResult",
    "dumps",
    "encode_ulid",
    "make_event",
    "parse_event",
    "to_dict",
    "ulid_ms",
    "ulid_timestamp",
]
