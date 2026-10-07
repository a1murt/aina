"""Helpers for the virtual plant tests (tests/sim, tests/integration)."""

from __future__ import annotations

import socket
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from qost_sim.calibration import CalibrationReport
from qost_sim.model import EventFactory, Inject, PlantModel, Rec
from twin_core.config import TwinConfig
from twin_core.events import dumps

CalibrationRun = tuple[CalibrationReport, PlantModel, list[Rec]]


def demo_start(cfg: TwinConfig) -> datetime:
    return cfg.simulation.clock.demo_start


def run(
    cfg: TwinConfig,
    *,
    start: datetime,
    until: datetime,
    seed: int | None = None,
    injects: Sequence[tuple[datetime, Inject]] = (),
    telemetry_period_s: float | None = None,
    chunks: Iterable[float] | None = None,
) -> tuple[PlantModel, list[Rec]]:
    """Run a model from ``start`` to ``until`` with scheduled injects; return all records."""
    model = PlantModel(cfg, start=start, seed=seed, telemetry_period_s=telemetry_period_s)
    for at, inject in injects:
        model.schedule(model.sec(at), inject)
    records: list[Rec] = []
    if chunks is not None:
        t = 0.0
        end = model.sec(until)
        for step in chunks:
            t = min(end, t + step)
            model.run_until(t)
            records.extend(model.drain())
            if t >= end:
                break
    model.run_until_time(until)
    records.extend(model.drain())
    return model, records


def to_jsonl(model: PlantModel, records: list[Rec], *, nonce: str = "test") -> str:
    factory = EventFactory.deterministic(site=model.site, t0=model.t0, seed=model.seed, nonce=nonce)
    return "".join(dumps(e) + "\n" for e in factory.convert_all(records))


def key(rec: Rec) -> tuple[object, ...]:
    return (round(rec.t, 6), rec.kind, rec.entity_type, rec.entity, sorted(rec.data.items()))


def of(records: Iterable[Rec], kind: str, entity: str | None = None) -> list[Rec]:
    return [r for r in records if r.kind == kind and (entity is None or r.entity == entity)]


def at(model: PlantModel, rec: Rec) -> datetime:
    return model.at(rec.t)


def minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port
