"""Tag values -> :mod:`twin_core.events` (SPEC §7.2, FR-ING-01/04). Pure: no I/O, no clock.

Values arrive in batches (one OPC UA publish response, or MQTT messages within a short grace);
:meth:`Normalizer.feed` buffers them and :meth:`Normalizer.flush` returns the events in arrival
order:

* ``State`` (Int32) -> ``state`` via the tag map's ``state_enum``; an unknown value -> DQ-07,
  dropped. ``State`` and ``AlarmCode`` written at the same SourceTimestamp are joined into one
  event (``alarm_code``; the engine maps it to a reason). An AlarmCode that arrives later for an
  already emitted state re-emits the same-instant state with the code; an AlarmCode change
  without a state change (a new stop cause) re-states the current state with the new code.
* ``Alarm`` -> ``alarm``; ``Level`` (+ ``Capacity``) -> ``buffer_level``; ``Kits`` -> ``ckd``
  (``consume``/``delivery`` by sign, ``set`` first); telemetry -> ``telemetry`` with its unit.
* The same node at the same timestamp twice in a batch keeps the last value; a value already seen
  (same timestamp and value) or older than the last one is a re-delivery and is dropped.
* Event ids are content-addressed (:func:`twin_core.events.content_ulid`), so OPC UA and MQTT
  give identical ids and re-deliveries are idempotent in the database.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from qost_collector.tagmap import Binding
from twin_core.clock import ensure_utc, format_utc
from twin_core.config import TwinConfig
from twin_core.db.sink import DqRecord
from twin_core.domain import EquipmentState
from twin_core.events import (
    AlarmData,
    AlarmEvent,
    AnyEvent,
    BufferLevelData,
    BufferLevelEvent,
    CkdData,
    CkdEvent,
    EventSource,
    Quality,
    StateData,
    StateEvent,
    TelemetryData,
    TelemetryEvent,
    content_ulid,
)

STOP_STATES = frozenset(
    {EquipmentState.DOWN_UNPLANNED, EquipmentState.DOWN_PLANNED, EquipmentState.CHANGEOVER}
)


Stamp = Callable[[], datetime | None]


class Stamper:
    """Strictly increasing ``received_ts`` (plant time) per collector process.

    Events of one entity at the same instant (e.g. the night state and the shift start, both at
    07:00:00) keep their arrival order in storage: replays order by ``(ts, received_ts,
    event_id)``.
    """

    def __init__(self, now: Callable[[], datetime | None]) -> None:
        self._now = now
        self.last: datetime | None = None

    def __call__(self) -> datetime | None:
        now = self._now()
        if now is None:
            return None
        if self.last is not None and now <= self.last:
            now = self.last + timedelta(microseconds=1)
        self.last = now
        return now


@dataclass(frozen=True, slots=True)
class Item:
    binding: Binding
    value: Any
    ts: datetime
    quality: Quality


@dataclass
class _Entity:
    state: EquipmentState | None = None
    state_ts: datetime | None = None
    alarm_code: str = ""
    last_code: str = ""
    awaiting_code: datetime | None = None


@dataclass
class Normalizer:
    """See the module docstring."""

    cfg: TwinConfig
    state_enum: Mapping[int, EquipmentState]
    source: EventSource
    site: str = ""
    pending: list[Item] = field(default_factory=list)
    last: dict[str, tuple[datetime, Any]] = field(default_factory=dict)
    entities: dict[str, _Entity] = field(default_factory=dict)
    capacity: dict[str, int] = field(default_factory=dict)
    kits: dict[str, int] = field(default_factory=dict)
    dq: list[DqRecord] = field(default_factory=list)
    _dq_seen: set[str] = field(default_factory=set)
    dropped: int = 0

    def __post_init__(self) -> None:
        self.site = self.site or self.cfg.plant.site.code
        for b in self.cfg.plant.buffers:
            self.capacity.setdefault(b.code, b.capacity)

    def reset(self) -> None:
        """Forget caches (simulator reset: values restart at demo_start)."""
        self.pending.clear()
        self.last.clear()
        self.entities.clear()
        self.kits.clear()

    # ------------------------------------------------------------------ input

    def feed(self, binding: Binding, value: Any, ts: datetime, quality: Quality = "good") -> None:
        self.pending.append(Item(binding, value, ensure_utc(ts), quality))

    def unknown(self, kind: str, value: str, ts: datetime, **context: Any) -> None:
        """DQ-07 for an unknown tag / topic / value (once per value and day)."""
        day = ts.date()
        key = f"DQ-07|{kind}|{value}|{day.isoformat()}"
        if key in self._dq_seen:
            return
        self._dq_seen.add(key)
        details: dict[str, Any] = {"kind": kind, "value": value, "suggestion": None, **context}
        self.dq.append(DqRecord(key, ts, "DQ-07", "warning", kind, day, details))

    def drain_dq(self) -> list[DqRecord]:
        out, self.dq = self.dq, []
        return out

    # ------------------------------------------------------------------ output

    def flush(self, received: datetime | Stamp | None) -> list[AnyEvent]:
        """Events of the buffered batch; ``received`` is a time or a :class:`Stamper`."""
        stamp: Stamp = received if callable(received) else (lambda: received)
        items, self.pending = self.pending, []
        # same node, same instant within the batch: the last value wins
        latest: dict[tuple[str, datetime], int] = {}
        for i, item in enumerate(items):
            latest[(item.binding.key, item.ts)] = i
        codes_at: dict[tuple[str, datetime], str] = {}
        states_at: set[tuple[str, datetime]] = set()
        kept: list[Item] = []
        for i, item in enumerate(items):
            if latest[(item.binding.key, item.ts)] != i:
                continue
            prev = self.last.get(item.binding.key)
            if prev is not None and (
                item.ts < prev[0] or (item.ts == prev[0] and item.value == prev[1])
            ):
                self.dropped += 1
                continue
            self.last[item.binding.key] = (item.ts, item.value)
            kept.append(item)
            b = item.binding
            if b.kind == "equipment" and b.signal == "alarm_code":
                codes_at[(b.code, item.ts)] = str(item.value or "")
            elif b.signal == "state":
                states_at.add((b.code, item.ts))
        out: list[AnyEvent] = []
        for item in kept:
            event = self._convert(item, stamp, codes_at, states_at)
            if event is not None:
                out.append(event)
        return out

    def _base(self, item: Item, kind: str, key_value: str, received: Stamp) -> dict[str, Any]:
        b = item.binding
        entity_type = "site" if b.kind == "site" else b.kind
        ts_us = format_utc(item.ts)
        return {
            "event_id": content_ulid(
                item.ts, f"{entity_type}|{b.code}|{kind}|{b.signal}|{ts_us}|{key_value}"
            ),
            "ts": item.ts,
            "received_ts": received(),
            "source": self.source,
            "site": self.site,
            "entity_type": entity_type,
            "entity": b.code,
            "quality": item.quality,
        }

    def _state_event(
        self, item: Item, state: EquipmentState, code: str, received: Stamp
    ) -> StateEvent:
        alarm_code = code or None
        base = self._base(item, "state", f"{state.value}|{code}", received)
        base["entity_type"] = item.binding.kind
        return StateEvent(**base, data=StateData(state=state, alarm_code=alarm_code))

    def _convert(
        self,
        item: Item,
        received: Stamp,
        codes_at: Mapping[tuple[str, datetime], str],
        states_at: set[tuple[str, datetime]],
    ) -> AnyEvent | None:
        b = item.binding
        if b.signal == "state":
            return self._on_state(item, received, codes_at)
        if b.kind == "equipment" and b.signal == "alarm_code":
            return self._on_alarm_code(item, received, states_at)
        if b.kind == "equipment" and b.signal == "alarm":
            ent = self.entities.setdefault(b.code, _Entity())
            code = ent.alarm_code or ent.last_code or "ALARM"
            reason = self.cfg.reasons.get(code)
            data = AlarmData(
                code=code, active=bool(item.value), text=reason.name_ru if reason else ""
            )
            base = self._base(item, "alarm", f"{code}|{bool(item.value)}", received)
            return AlarmEvent(**base, data=data)
        if b.telemetry:
            try:
                value = float(item.value)
            except (TypeError, ValueError):
                self.unknown("value", f"{b.key}={item.value!r}", item.ts)
                return None
            base = self._base(item, "telemetry", repr(value), received)
            return TelemetryEvent(
                **base, data=TelemetryData(signal=b.signal, value=value, unit=b.unit or "")
            )
        if b.kind == "buffer":
            if b.signal == "capacity":
                with contextlib.suppress(TypeError, ValueError):
                    self.capacity[b.code] = max(int(item.value), 1)
                return None
            level = max(int(item.value), 0)
            level_data = BufferLevelData(
                buffer=b.code, level=level, capacity=self.capacity.get(b.code, max(level, 1))
            )
            return BufferLevelEvent(
                **self._base(item, "buffer_level", str(level), received), data=level_data
            )
        if b.kind == "product" and b.signal == "kits":
            kits = max(int(item.value), 0)
            prev = self.kits.get(b.code)
            self.kits[b.code] = kits
            kind = "set" if prev is None else ("consume" if kits < prev else "delivery")
            if prev is not None and kits == prev:
                return None
            ckd = CkdData(product=b.code, kits=kits, event=kind)  # type: ignore[arg-type]
            return CkdEvent(**self._base(item, "ckd", f"{kits}|{kind}", received), data=ckd)
        return None

    def _on_state(
        self, item: Item, received: Stamp, codes_at: Mapping[tuple[str, datetime], str]
    ) -> StateEvent | None:
        b = item.binding
        try:
            state = self.state_enum[int(item.value)]
        except (KeyError, TypeError, ValueError):
            self.unknown("state_value", f"{b.key}={item.value!r}", item.ts, tag=b.key)
            return None
        ent = self.entities.setdefault(b.code, _Entity())
        code = ""
        if b.kind == "equipment":
            if (b.code, item.ts) in codes_at:
                code = codes_at[(b.code, item.ts)]
            elif state in STOP_STATES:
                ent.awaiting_code = item.ts
            if state not in STOP_STATES:
                code = ""
            ent.alarm_code = code
            if code:
                ent.last_code = code
        ent.state, ent.state_ts = state, item.ts
        return self._state_event(item, state, code, received)

    def _on_alarm_code(
        self, item: Item, received: Stamp, states_at: set[tuple[str, datetime]]
    ) -> StateEvent | None:
        b = item.binding
        code = str(item.value or "")
        ent = self.entities.setdefault(b.code, _Entity())
        if (b.code, item.ts) in states_at:
            return None  # joined with the state of the same instant
        if ent.state is None or ent.state not in STOP_STATES or not code:
            ent.alarm_code = code
            return None
        if code == ent.alarm_code and ent.awaiting_code is None:
            return None
        ent.alarm_code = code
        ent.last_code = code
        at = item.ts
        if ent.awaiting_code is not None and ent.state_ts == item.ts:
            at = ent.state_ts  # late join of the same instant
        ent.awaiting_code = None
        state_item = Item(Binding(b.key, "equipment", b.code, "state"), None, at, item.quality)
        return self._state_event(state_item, ent.state, code, received)


def mqtt_payload(raw: bytes | str) -> tuple[Any, datetime | None, Quality]:
    """``{"ts": "...Z", "value": ..., "quality": "good"}`` -> (value, ts, quality)."""
    body = json.loads(raw)
    ts_raw = body.get("ts")
    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if isinstance(ts_raw, str) else None
    quality = body.get("quality", "good")
    if quality not in ("good", "uncertain", "bad"):
        quality = "uncertain"
    return body.get("value"), ts, quality


__all__ = ["Item", "Normalizer", "Stamp", "Stamper", "mqtt_payload"]
