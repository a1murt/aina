"""``db:`` event sink: batched, idempotent writes of events into TimescaleDB (FR-ING-02/03).

Shared by the collector (live) and ``qost_sim backfill --sink db:`` (history). One transaction
per batch:

1. ``event_raw`` — ``INSERT … SELECT FROM unnest(arrays) ON CONFLICT DO NOTHING RETURNING
   event_id``;
2. the 1:1 facts of the *newly inserted* events only — ``telemetry`` and ``unit_event`` (``DO
   NOTHING``), ``buffer_level`` and ``ckd_stock`` (last value per key wins, ``DO UPDATE``),
   ``defect`` (no natural key; idempotent because it is gated by step 1).

Replaying a batch (spool, re-run backfill) therefore never duplicates anything. Raw asyncpg with
array parameters (one statement per table, no per-row SQL) keeps the write path fast.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import asyncpg

from twin_core.event_sink import register_sink
from twin_core.events import (
    AnyEvent,
    BufferLevelEvent,
    CkdEvent,
    DefectEvent,
    TelemetryEvent,
    UnitEvent,
)


def asyncpg_dsn(url: str) -> str:
    """``postgresql+asyncpg://…`` (SQLAlchemy) -> ``postgresql://…`` (asyncpg)."""
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


@dataclass
class Rows:
    """Column arrays of one batch, per table (pure; built without a database)."""

    raw: list[list[Any]] = field(default_factory=lambda: [[] for _ in range(9)])
    telemetry: list[tuple[str, list[Any]]] = field(default_factory=list)
    units: list[tuple[str, list[Any]]] = field(default_factory=list)
    buffers: dict[tuple[str, datetime], tuple[str, int]] = field(default_factory=dict)
    ckd: dict[tuple[str, datetime], tuple[str, int]] = field(default_factory=dict)
    defects: list[tuple[str, list[Any]]] = field(default_factory=list)


def build_rows(events: Sequence[AnyEvent], area_of_line: Mapping[str, str]) -> Rows:
    """Split a batch into table rows (facts keyed by the event id that produced them)."""
    rows = Rows()
    raw = rows.raw
    for ev in events:
        raw[0].append(ev.event_id)
        raw[1].append(ev.ts)
        raw[2].append(ev.received_ts)
        raw[3].append(ev.source)
        raw[4].append(ev.entity_type)
        raw[5].append(ev.entity)
        raw[6].append(ev.kind)
        raw[7].append(ev.data.model_dump_json())
        raw[8].append(ev.quality)
        if isinstance(ev, TelemetryEvent):
            if ev.entity_type == "equipment":
                rows.telemetry.append(
                    (ev.event_id, [ev.entity, ev.data.signal, ev.ts, ev.data.value, ev.quality])
                )
        elif isinstance(ev, UnitEvent):
            d = ev.data
            rows.units.append(
                (ev.event_id, [d.line, d.body_id, ev.ts, d.product, d.result, d.defect_code])
            )
        elif isinstance(ev, BufferLevelEvent):
            rows.buffers[(ev.data.buffer, ev.ts)] = (ev.event_id, ev.data.level)
        elif isinstance(ev, CkdEvent):
            rows.ckd[(ev.data.product, ev.ts)] = (ev.event_id, ev.data.kits)
        elif isinstance(ev, DefectEvent):
            dd = ev.data
            rows.defects.append(
                (
                    ev.event_id,
                    [
                        ev.ts,
                        dd.line,
                        area_of_line.get(dd.line, dd.line),
                        dd.body_id,
                        dd.defect_code,
                        dd.qty,
                        dd.disposition,
                        ev.source,
                    ],
                )
            )
    return rows


def _columns(items: Sequence[Sequence[Any]], width: int) -> list[list[Any]]:
    cols: list[list[Any]] = [[] for _ in range(width)]
    for item in items:
        for i, value in enumerate(item):
            cols[i].append(value)
    return cols


_RAW_SQL = """
INSERT INTO event_raw (event_id, ts, received_ts, source, entity_type, entity, kind, data, quality)
SELECT u.event_id, u.ts, u.received_ts, u.source, u.entity_type, u.entity, u.kind, u.data::jsonb,
       u.quality
FROM unnest($1::text[], $2::timestamptz[], $3::timestamptz[], $4::text[], $5::text[], $6::text[],
            $7::text[], $8::text[], $9::text[])
     AS u(event_id, ts, received_ts, source, entity_type, entity, kind, data, quality)
ON CONFLICT DO NOTHING
RETURNING event_id
"""
_TELEMETRY_SQL = """
INSERT INTO telemetry (equipment, signal, ts, value, quality)
SELECT * FROM unnest($1::text[], $2::text[], $3::timestamptz[], $4::float8[], $5::text[])
ON CONFLICT DO NOTHING
"""
_UNIT_SQL = """
INSERT INTO unit_event (line, body_id, ts, product, result, defect_code)
SELECT * FROM unnest($1::text[], $2::text[], $3::timestamptz[], $4::text[], $5::text[], $6::text[])
ON CONFLICT DO NOTHING
"""
_BUFFER_SQL = """
INSERT INTO buffer_level (buffer, ts, level)
SELECT * FROM unnest($1::text[], $2::timestamptz[], $3::int[])
ON CONFLICT (buffer, ts) DO UPDATE SET level = EXCLUDED.level
"""
_CKD_SQL = """
INSERT INTO ckd_stock (product, ts, kits)
SELECT * FROM unnest($1::text[], $2::timestamptz[], $3::int[])
ON CONFLICT (product, ts) DO UPDATE SET kits = EXCLUDED.kits
"""
_DEFECT_SQL = """
INSERT INTO defect (ts, line, area, body_id, defect_code, qty, disposition, source)
SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[], $6::int[],
                     $7::text[], $8::text[])
"""
_DQ_SQL = """
INSERT INTO dq_issue (ts, rule_id, severity, entity, period_date, details, status, dedup_key)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'open', $7)
ON CONFLICT (dedup_key) DO UPDATE SET ts = EXCLUDED.ts, details = EXCLUDED.details,
    severity = EXCLUDED.severity
"""


@dataclass(frozen=True, slots=True)
class DqRecord:
    """A data-quality finding to upsert by ``dedup_key`` (collector DQ-07)."""

    dedup_key: str
    ts: datetime
    rule_id: str
    severity: str
    entity: str
    period_date: date | None
    details: Mapping[str, Any]


class DbSink:
    """Event sink writing into the database (see the module docstring)."""

    def __init__(
        self,
        url: str,
        *,
        area_of_line: Mapping[str, str] | None = None,
        pool_size: int = 2,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.dsn = asyncpg_dsn(url)
        self._area_of_line = area_of_line
        self.pool_size = pool_size
        self.connect_timeout_s = connect_timeout_s
        self._pool: asyncpg.Pool | None = None
        self.written = 0
        self.inserted = 0
        self.batches = 0

    def _areas(self) -> Mapping[str, str]:
        if self._area_of_line is None:
            from twin_core.config import load_config_from_settings

            cfg = load_config_from_settings()
            self._area_of_line = {line: cfg.area_of_line(line).code for line in cfg.lines}
        return self._area_of_line

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=1,
                max_size=self.pool_size,
                timeout=self.connect_timeout_s,
                command_timeout=60,
            )
        return self._pool

    async def reset_pool(self) -> None:
        """Drop the pool after a connection failure (the next write reconnects)."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.terminate()

    async def write(self, events: Sequence[AnyEvent]) -> None:
        await self.write_batch(events)

    async def write_batch(self, events: Sequence[AnyEvent]) -> int:
        """Write one batch in one transaction; returns the number of new events."""
        if not events:
            return 0
        rows = build_rows(events, self._areas())
        pool = await self.pool()
        async with pool.acquire() as conn, conn.transaction():
            records = await conn.fetch(_RAW_SQL, *rows.raw)
            new = {r["event_id"] for r in records}
            await self._facts(conn, rows, new)
        self.written += len(events)
        self.inserted += len(new)
        self.batches += 1
        return len(new)

    async def _facts(self, conn: asyncpg.Connection, rows: Rows, new: set[str]) -> None:
        def fresh(items: Sequence[tuple[str, list[Any]]]) -> list[list[Any]]:
            return [values for event_id, values in items if event_id in new]

        telemetry = fresh(rows.telemetry)
        if telemetry:
            await conn.execute(_TELEMETRY_SQL, *_columns(telemetry, 5))
        units = fresh(rows.units)
        if units:
            await conn.execute(_UNIT_SQL, *_columns(units, 6))
        buffers = [
            [code, ts, level]
            for (code, ts), (event_id, level) in rows.buffers.items()
            if event_id in new
        ]
        if buffers:
            await conn.execute(_BUFFER_SQL, *_columns(buffers, 3))
        ckd = [
            [code, ts, kits] for (code, ts), (event_id, kits) in rows.ckd.items() if event_id in new
        ]
        if ckd:
            await conn.execute(_CKD_SQL, *_columns(ckd, 3))
        defects = fresh(rows.defects)
        if defects:
            await conn.execute(_DEFECT_SQL, *_columns(defects, 8))

    async def write_dq(self, records: Sequence[DqRecord]) -> None:
        if not records:
            return
        pool = await self.pool()
        async with pool.acquire() as conn, conn.transaction():
            for r in records:
                await conn.execute(
                    _DQ_SQL,
                    r.ts,
                    r.rule_id,
                    r.severity,
                    r.entity,
                    r.period_date,
                    json.dumps(dict(r.details), ensure_ascii=False, default=str),
                    r.dedup_key,
                )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _factory(arg: str) -> DbSink:
    url = arg or os.environ.get("DATABASE_URL", "")
    if not url:
        raise ValueError("db sink needs a URL: 'db:postgresql+asyncpg://…' or DATABASE_URL")
    return DbSink(url)


register_sink("db", _factory)

__all__ = ["DbSink", "DqRecord", "Rows", "asyncpg_dsn", "build_rows"]
