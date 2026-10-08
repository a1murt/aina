"""Engine test helpers: event builders on 2026-10-16 (shift A = 02:00-10:00 UTC)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from qost_engine.core import EngineCore
from qost_engine.core.effects import Effect
from twin_core.config import TwinConfig
from twin_core.events import AnyEvent, RandomIds, make_event

DAY0 = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
SHIFT_A = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
SHIFT_B = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
_IDS = RandomIds()

LINES = ("WELD-1", "PAINT-1", "ASSY-1", "QC-1")


def at(minutes: float, base: datetime = SHIFT_A) -> datetime:
    return base + timedelta(minutes=minutes)


def ev(
    kind: str, entity_type: str, entity: str, data: dict[str, Any], ts: datetime, **kw: Any
) -> AnyEvent:
    return make_event(
        kind,  # type: ignore[arg-type]
        event_id=_IDS(ts),
        ts=ts,
        source=kw.pop("source", "opcua"),
        site="KST",
        entity_type=entity_type,  # type: ignore[arg-type]
        entity=entity,
        data=data,
        **kw,
    )


def state(entity: str, value: str, ts: datetime, *, line: bool = False, **data: Any) -> AnyEvent:
    return ev("state", "line" if line else "equipment", entity, {"state": value, **data}, ts)


def unit(
    line: str, ts: datetime, result: str = "pass", product: str = "ONIX", n: int = 0
) -> AnyEvent:
    return ev(
        "unit",
        "line",
        line,
        {"line": line, "body_id": f"B{ts:%y%m%d}{n:04d}", "product": product, "result": result},
        ts,
    )


def buffer(code: str, level: int, ts: datetime, capacity: int = 30) -> AnyEvent:
    return ev(
        "buffer_level", "buffer", code, {"buffer": code, "level": level, "capacity": capacity}, ts
    )


def start_plant(cfg: TwinConfig, ts: datetime = SHIFT_A) -> list[AnyEvent]:
    """Every unit and line RUNNING at ``ts``."""
    events = [state(code, "RUNNING", ts) for code in cfg.equipment]
    events += [state(line, "RUNNING", ts, line=True) for line in LINES]
    events += [buffer(b.code, b.capacity // 2, ts, b.capacity) for b in cfg.plant.buffers]
    return events


def run(core: EngineCore, events: list[AnyEvent]) -> list[Effect]:
    out: list[Effect] = []
    for e in events:
        core.apply(e)
        out.extend(core.drain()[0])
    return out


def of_type(effects: list[Effect], cls: type) -> list[Any]:
    return [e for e in effects if isinstance(e, cls)]
