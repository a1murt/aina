"""Replay driver: recompute the derived tables of a period from stored events (SPEC §6.1, §9).

``make history`` = ``qost_sim backfill --sink db:`` followed by ``qost_engine replay``: events
of ``[from, to)`` are read from ``event_raw`` (telemetry excluded) in time order, day by day,
through the same :class:`EngineCore` as live (``mode=replay``: no publishing, no close grace);
the coalesced effects of each day are written in one transaction. At the end the core's state is
stored as the ``live`` checkpoint (the live engine continues from it) and as ``baseline`` (what
the demo reset returns to).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import structlog

from qost_engine.core import EngineCore
from qost_engine.core.effects import Effect
from qost_engine.writer import Checkpoint, EngineWriter, apply_effects, coalesce, delete_derived
from twin_core.config import TwinConfig
from twin_core.events import EVENT_CLASSES, AnyEvent, parse_event

log = structlog.get_logger("qost_engine.replay")

BASELINE = "baseline"
_CHUNK = timedelta(days=1)
_EVENTS_SQL = """
SELECT event_id, ts, received_ts, source, entity_type, entity, kind, data::text AS data, quality
FROM event_raw
WHERE ts >= $1 AND ts < $2 AND kind <> 'telemetry'
ORDER BY ts, received_ts NULLS FIRST, event_id
"""


@dataclass
class ReplayStats:
    start: datetime
    end: datetime
    events: int = 0
    effects: int = 0
    rows: int = 0
    deleted: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    read_s: float = 0.0
    core_s: float = 0.0
    write_s: float = 0.0
    core: dict[str, Any] = field(default_factory=dict)


def event_from_row(row: Any, site: str) -> AnyEvent:
    cls = EVENT_CLASSES[row["kind"]]
    event: AnyEvent = cls.model_validate(  # type: ignore[assignment]
        {
            "event_id": row["event_id"],
            "ts": row["ts"],
            "received_ts": row["received_ts"],
            "source": row["source"],
            "site": site,
            "entity_type": row["entity_type"],
            "entity": row["entity"],
            "kind": row["kind"],
            "data": json.loads(row["data"]),
            "quality": row["quality"],
        }
    )
    return event


async def db_events(
    conn: asyncpg.Connection, site: str, lo: datetime, hi: datetime
) -> AsyncIterator[list[AnyEvent]]:
    """Non-telemetry events of ``[lo, hi)`` per day chunk, ordered by (ts, event_id)."""
    cursor = lo
    while cursor < hi:
        upper = min(cursor + _CHUNK, hi)
        rows = await conn.fetch(_EVENTS_SQL, cursor, upper)
        yield [event_from_row(r, site) for r in rows]
        cursor = upper


def jsonl_events(path: Path, lo: datetime, hi: datetime) -> Iterable[AnyEvent]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if '"kind":"telemetry"' in line:
                continue
            event = parse_event(line)
            if lo <= event.ts < hi:
                yield event


async def run_replay(
    cfg: TwinConfig,
    *,
    database_url: str,
    start: datetime | None = None,
    end: datetime | None = None,
    jsonl: Path | None = None,
    resolve_history_alerts: bool = True,
    checkpoint: str = "live",
    line_state_source: str = "events",
) -> ReplayStats:
    clock = cfg.simulation.clock
    lo = (start or clock.backfill_from).astimezone(UTC)
    hi = (end or clock.demo_start).astimezone(UTC)
    began = time.perf_counter()
    stats = ReplayStats(lo, hi)
    core = EngineCore(
        cfg,
        mode="replay",
        resolve_history_alerts=resolve_history_alerts,
        line_state_source="derive" if line_state_source == "derive" else "events",
    )
    writer = EngineWriter(database_url, cfg=cfg)
    pool = await writer.pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                stats.deleted = await delete_derived(conn, cfg, lo, hi)
                await conn.execute(
                    "DELETE FROM engine_checkpoint WHERE name = ANY($1::text[])",
                    [checkpoint, BASELINE],
                )

            async def flush(effects: list[Effect]) -> None:
                t0 = time.perf_counter()
                items = coalesce(effects)
                stats.effects += len(effects)
                stats.rows += len(items)
                async with conn.transaction():
                    await apply_effects(conn, items, cfg)
                stats.write_s += time.perf_counter() - t0

            async def feed(batch: Iterable[AnyEvent]) -> None:
                t0 = time.perf_counter()
                for event in batch:
                    core.apply(event)
                    stats.events += 1
                stats.core_s += time.perf_counter() - t0
                effects, _live = core.drain()
                await flush(effects)

            if jsonl is not None:
                await feed(jsonl_events(jsonl, lo, hi))
            else:
                t_read = time.perf_counter()
                async for batch in db_events(conn, cfg.plant.site.code, lo, hi):
                    stats.read_s += time.perf_counter() - t_read
                    await feed(batch)
                    t_read = time.perf_counter()
            core.advance_to(hi)
            effects, _live = core.drain()
            await flush(effects)
        state = core.snapshot()
        cps = [
            Checkpoint(checkpoint, None, hi, state, hi),
            Checkpoint(BASELINE, None, hi, state, hi),
        ]
        await writer.commit([], cps)
    finally:
        await writer.aclose()
    stats.core = core.stats
    stats.seconds = time.perf_counter() - began
    log.info(
        "replay_done",
        events=stats.events,
        rows=stats.rows,
        seconds=round(stats.seconds, 2),
        read_s=round(stats.read_s, 2),
        core_s=round(stats.core_s, 2),
        write_s=round(stats.write_s, 2),
    )
    return stats


__all__ = ["BASELINE", "ReplayStats", "db_events", "event_from_row", "run_replay"]
