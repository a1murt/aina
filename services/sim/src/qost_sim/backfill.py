"""Backfill mode (SPEC §6.1): history from ``clock.backfill_from`` to ``clock.demo_start``.

Runs the same deterministic model as live mode at full speed (telemetry every
``telemetry.backfill_sample_period_s``), converts records to :mod:`twin_core.events` with
deterministic ids (nonce ``backfill``: re-running is idempotent under ``ON CONFLICT DO NOTHING``)
and writes them to an :class:`~twin_core.event_sink.EventSink` in batches.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from qost_sim.model import EventFactory, PlantModel
from twin_core.config import TwinConfig
from twin_core.event_sink import EventSink

BACKFILL_NONCE = "backfill"
_CHUNK = timedelta(hours=1)


@dataclass
class BackfillStats:
    start: datetime
    end: datetime
    events: int = 0
    batches: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)
    seconds: float = 0.0


async def run_backfill(
    cfg: TwinConfig,
    sink: EventSink,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    seed: int | None = None,
    telemetry_period_s: float | None = None,
    batch_size: int = 500,
) -> BackfillStats:
    clock = cfg.simulation.clock
    start = start or clock.backfill_from
    end = end or clock.demo_start
    if end <= start:
        raise ValueError(f"backfill end {end.isoformat()} is not after start {start.isoformat()}")
    period = telemetry_period_s or cfg.simulation.telemetry.backfill_sample_period_s
    began = time.perf_counter()
    model = PlantModel(cfg, start=start, seed=seed, telemetry_period_s=period)
    factory = EventFactory.deterministic(
        site=cfg.plant.site.code, t0=model.t0, seed=model.seed, nonce=BACKFILL_NONCE
    )
    stats = BackfillStats(start, end)
    cursor = start
    pending = []
    while cursor < end:
        cursor = min(cursor + _CHUNK, end)
        model.run_until_time(cursor)
        for event in factory.convert_all(model.drain()):
            pending.append(event)
            stats.by_kind[event.kind] += 1
            if len(pending) >= batch_size:
                await sink.write(pending)
                stats.events += len(pending)
                stats.batches += 1
                pending = []
    if pending:
        await sink.write(pending)
        stats.events += len(pending)
        stats.batches += 1
    stats.seconds = time.perf_counter() - began
    return stats
