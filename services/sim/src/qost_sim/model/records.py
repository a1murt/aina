"""Compact internal records emitted by the model and their conversion to ``twin_core.events``.

The model appends :class:`Rec` objects to its outbox (cheap, no validation in the hot path);
outputs convert them at the boundary: :class:`EventFactory` builds validated
:mod:`twin_core.events` objects with deterministic ULIDs in emission order. ``oracle`` records
(the hidden wear ``Degradation``) never become events (FR-SIM-02).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from twin_core.events import EVENT_CLASSES, AnyEvent, DeterministicIds, EventIds

ORACLE = "oracle"


@dataclass(slots=True)
class Rec:
    t: float
    """Plant seconds since the model start."""
    kind: str
    entity_type: str
    entity: str
    data: dict[str, Any]
    extra: dict[str, Any] | None = None
    """Output-only details (counters, cycle time); not part of the event payload."""


class EventFactory:
    """Converts records to events: ``ts = t0 + t``, ``source = sim``, ``received_ts = ts``."""

    def __init__(self, *, site: str, t0: datetime, ids: EventIds) -> None:
        self.site = site
        self.t0 = t0
        self.ids = ids

    @classmethod
    def deterministic(cls, *, site: str, t0: datetime, seed: int, nonce: str) -> EventFactory:
        return cls(site=site, t0=t0, ids=DeterministicIds(seed, nonce))

    def ts(self, rec: Rec) -> datetime:
        return self.t0 + timedelta(seconds=rec.t)

    def convert(self, rec: Rec) -> AnyEvent | None:
        if rec.kind == ORACLE:
            return None
        ts = self.ts(rec)
        cls = EVENT_CLASSES[rec.kind]
        return cls.model_validate(  # type: ignore[return-value]
            {
                "event_id": self.ids(ts),
                "ts": ts,
                "received_ts": ts,
                "source": "sim",
                "site": self.site,
                "entity_type": rec.entity_type,
                "entity": rec.entity,
                "kind": rec.kind,
                "data": rec.data,
            }
        )

    def convert_all(self, records: list[Rec]) -> list[AnyEvent]:
        events: list[AnyEvent] = []
        for rec in records:
            event = self.convert(rec)
            if event is not None:
                events.append(event)
        return events
