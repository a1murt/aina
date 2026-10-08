"""Database side of the engine: applies core effects in one transaction with the checkpoint,
deletes derived rows for replays and the demo reset (asyncpg)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import asyncpg

from qost_engine.core.effects import (
    AlertEscalate,
    AlertUpsert,
    AuditRow,
    BottleneckRow,
    DowntimeRow,
    DqUpsert,
    Effect,
    KpiShiftRow,
    ReclassifyRequest,
    StateInterval,
)
from twin_core.config import TwinConfig
from twin_core.db.sink import asyncpg_dsn


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    name: str
    stream_id: str | None
    event_ts: datetime | None
    state: dict[str, Any] | None
    updated_ts: datetime


def coalesce(effects: Iterable[Effect]) -> list[Effect]:
    """Keep the last effect per key, in first-seen key order (escalations after upserts)."""
    merged: dict[tuple[str, ...], Effect] = {}
    for effect in effects:
        merged.pop(effect.key, None)
        merged[effect.key] = effect
    ordered = list(merged.values())
    return [e for e in ordered if not isinstance(e, AlertEscalate)] + [
        e for e in ordered if isinstance(e, AlertEscalate)
    ]


_STATE = """
INSERT INTO equipment_state (entity, start_ts, end_ts, entity_type, state, reason_code, source)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (entity, start_ts) DO UPDATE SET end_ts = EXCLUDED.end_ts, state = EXCLUDED.state,
    reason_code = EXCLUDED.reason_code, source = EXCLUDED.source
"""
_DOWNTIME = """
INSERT INTO downtime (entity, line, start_ts, end_ts, duration_s, planned, microstop, reason_code,
    reason_source, shift_date, shift_code, comment, classified_ts)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
ON CONFLICT (entity, start_ts) WHERE import_id IS NULL AND start_ts IS NOT NULL
DO UPDATE SET end_ts = EXCLUDED.end_ts, duration_s = EXCLUDED.duration_s,
    planned = EXCLUDED.planned, microstop = EXCLUDED.microstop,
    reason_code = EXCLUDED.reason_code, reason_source = EXCLUDED.reason_source,
    comment = COALESCE(EXCLUDED.comment, downtime.comment),
    classified_ts = COALESCE(EXCLUDED.classified_ts, downtime.classified_ts)
"""
_KPI_COLUMNS = [
    "pot",
    "pdot",
    "pbt",
    "apt",
    "adot",
    "adet",
    "aust",
    "microstop_min",
    "pq",
    "gq",
    "pri_good_s",
    "availability",
    "effectiveness",
    "quality_ratio",
    "oee",
    "fpy",
    "defect_rate",
    "failures",
    "repair_min",
]
_KPI = f"""
INSERT INTO kpi_shift (line, shift_date, shift_code, source, version, final, computed_ts,
    {", ".join(_KPI_COLUMNS)})
VALUES ($1, $2, $3, 'events', $4, $5, $6,
    {", ".join(f"${i}" for i in range(7, 7 + len(_KPI_COLUMNS)))})
ON CONFLICT (line, shift_date, shift_code, source, version) DO UPDATE SET final = EXCLUDED.final,
    computed_ts = EXCLUDED.computed_ts,
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _KPI_COLUMNS)}
"""
_BOTTLENECK = """
INSERT INTO bottleneck_shift (line_group, shift_date, shift_code, line, sole_share, shifting_share)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (line_group, shift_date, shift_code, line) DO UPDATE SET
    sole_share = EXCLUDED.sole_share, shifting_share = EXCLUDED.shifting_share
"""
_ALERT = """
INSERT INTO alert (ts, rule_id, severity, entity_type, entity, title_ru, message_ru, value, status,
    resolved_ts, escalation_level, dedup_key)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, 0, $11)
ON CONFLICT (dedup_key) DO UPDATE SET severity = EXCLUDED.severity, value = EXCLUDED.value,
    title_ru = EXCLUDED.title_ru, message_ru = EXCLUDED.message_ru,
    status = CASE WHEN EXCLUDED.status = 'resolved' THEN 'resolved'
                  WHEN alert.status = 'ack' THEN 'ack' ELSE EXCLUDED.status END,
    resolved_ts = CASE WHEN EXCLUDED.status = 'resolved' THEN EXCLUDED.resolved_ts ELSE NULL END
"""
_ESCALATE = """
UPDATE alert SET escalation_level = $2
WHERE dedup_key = $1 AND status = 'open' AND escalation_level < $2
RETURNING id
"""
_DQ = """
INSERT INTO dq_issue (ts, rule_id, severity, entity, period_date, details, status, dedup_key)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'open', $7)
ON CONFLICT (dedup_key) DO UPDATE SET ts = EXCLUDED.ts, severity = EXCLUDED.severity,
    details = EXCLUDED.details
"""
_AUDIT = """
INSERT INTO audit_log (ts, user_id, action, entity_type, entity_id, before, after)
VALUES ($1, NULL, $2, $3, $4, $5::jsonb, $6::jsonb)
"""
_CHECKPOINT = """
INSERT INTO engine_checkpoint (name, stream_id, event_ts, state, updated_ts)
VALUES ($1, $2, $3, $4::jsonb, $5)
ON CONFLICT (name) DO UPDATE SET stream_id = EXCLUDED.stream_id, event_ts = EXCLUDED.event_ts,
    state = EXCLUDED.state, updated_ts = EXCLUDED.updated_ts
"""


async def _kpi_insert(
    conn: asyncpg.Connection,
    line: str,
    shift_date: Any,
    code: str,
    version: int,
    values: dict[str, Any],
    computed: datetime,
) -> None:
    await conn.execute(
        _KPI,
        line,
        shift_date,
        code,
        version,
        True,
        computed,
        *[values.get(c) for c in _KPI_COLUMNS],
    )


def _kpi_args(e: KpiShiftRow) -> list[Any]:
    v = e.values
    return [
        e.line,
        e.shift_date,
        e.shift_code,
        e.version,
        e.final,
        e.computed_ts,
        *[v.get(c) for c in _KPI_COLUMNS],
    ]


class EngineWriter:
    """Applies effects (coalesced) and the checkpoint in one transaction."""

    def __init__(self, url: str, *, pool_size: int = 3, cfg: TwinConfig | None = None) -> None:
        self.cfg = cfg
        self.dsn = asyncpg_dsn(url)
        self.pool_size = pool_size
        self._pool: asyncpg.Pool | None = None

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn, min_size=1, max_size=self.pool_size, timeout=5.0, command_timeout=120
            )
        return self._pool

    async def reset_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.terminate()

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def commit(
        self, effects: Sequence[Effect], checkpoints: Sequence[Checkpoint] = ()
    ) -> list[AlertEscalate]:
        """Write effects + checkpoints atomically; returns the escalations that took effect."""
        items = coalesce(effects)
        pool = await self.pool()
        async with pool.acquire() as conn, conn.transaction():
            escalated = await apply_effects(conn, items, self.cfg)
            for cp in checkpoints:
                await write_checkpoint(conn, cp)
        return escalated

    async def load_checkpoint(self, name: str) -> Checkpoint | None:
        pool = await self.pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, stream_id, event_ts, state, updated_ts FROM engine_checkpoint "
                "WHERE name = $1",
                name,
            )
        if row is None:
            return None
        state = row["state"]
        return Checkpoint(
            row["name"],
            row["stream_id"],
            row["event_ts"],
            json.loads(state) if isinstance(state, str) else state,
            row["updated_ts"],
        )

    async def alert_counts(self) -> dict[str, int]:
        pool = await self.pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT severity, count(*) AS n FROM alert WHERE status = 'open' GROUP BY severity"
            )
        counts = {"critical": 0, "warning": 0, "info": 0}
        for r in rows:
            counts[r["severity"]] = int(r["n"])
        return counts


async def apply_effects(
    conn: asyncpg.Connection, items: Sequence[Effect], cfg: TwinConfig | None = None
) -> list[AlertEscalate]:
    states = [e for e in items if isinstance(e, StateInterval)]
    if states:
        await conn.executemany(
            _STATE,
            [
                (e.entity, e.start, e.end, e.entity_type, e.state, e.reason_code, e.source)
                for e in states
            ],
        )
    downtimes = [e for e in items if isinstance(e, DowntimeRow)]
    if downtimes:
        await conn.executemany(
            _DOWNTIME,
            [
                (
                    e.entity,
                    e.line,
                    e.start,
                    e.end,
                    e.duration_s,
                    e.planned,
                    e.microstop,
                    e.reason_code,
                    e.reason_source,
                    e.shift_date,
                    e.shift_code,
                    e.comment,
                    e.classified_ts,
                )
                for e in downtimes
            ],
        )
    kpis = [e for e in items if isinstance(e, KpiShiftRow)]
    if kpis:
        await conn.executemany(_KPI, [_kpi_args(e) for e in kpis])
    bns = [e for e in items if isinstance(e, BottleneckRow)]
    if bns:
        await conn.executemany(
            _BOTTLENECK,
            [
                (e.line_group, e.shift_date, e.shift_code, e.line, e.sole_share, e.shifting_share)
                for e in bns
            ],
        )
    alerts = [e for e in items if isinstance(e, AlertUpsert)]
    if alerts:
        await conn.executemany(
            _ALERT,
            [
                (
                    e.ts,
                    e.rule_id,
                    e.severity,
                    e.entity_type,
                    e.entity,
                    e.title_ru,
                    e.message_ru,
                    _json(e.value),
                    e.status,
                    e.resolved_ts,
                    e.dedup_key,
                )
                for e in alerts
            ],
        )
    dqs = [e for e in items if isinstance(e, DqUpsert)]
    if dqs:
        await conn.executemany(
            _DQ,
            [
                (
                    e.ts,
                    e.rule_id,
                    e.severity,
                    e.entity,
                    e.period_date,
                    _json(e.details),
                    e.dedup_key,
                )
                for e in dqs
            ],
        )
    audits = [e for e in items if isinstance(e, AuditRow)]
    if audits:
        await conn.executemany(
            _AUDIT,
            [
                (
                    e.ts,
                    e.action,
                    e.entity_type,
                    e.entity_id,
                    _json(e.before) if e.before is not None else None,
                    _json({**(e.after or {}), "by": e.user, "source": "engine"}),
                )
                for e in audits
            ],
        )
    reclassify = [e for e in items if isinstance(e, ReclassifyRequest)]
    if reclassify and cfg is not None:
        from qost_engine.recompute import reclassify_stored

        for request in reclassify:
            await reclassify_stored(conn, cfg, request)
    escalated: list[AlertEscalate] = []
    for e in items:
        if isinstance(e, AlertEscalate):
            row = await conn.fetchrow(_ESCALATE, e.dedup_key, e.level)
            if row is not None:
                escalated.append(e)
    return escalated


async def write_checkpoint(conn: asyncpg.Connection, cp: Checkpoint) -> None:
    await conn.execute(
        _CHECKPOINT,
        cp.name,
        cp.stream_id,
        cp.event_ts,
        _json(cp.state) if cp.state is not None else None,
        cp.updated_ts,
    )


async def delete_derived(
    conn: asyncpg.Connection,
    cfg: TwinConfig,
    lo: datetime,
    hi: datetime | None = None,
    *,
    reopen: bool = False,
) -> dict[str, int]:
    """Delete engine-derived rows in ``[lo, hi)`` (replay) or ``[lo, ∞)`` (reset, ``reopen``:
    intervals and stops running across ``lo`` become open again — the state at ``lo``)."""
    hi_cond = "" if hi is None else " AND {col} < $2"
    args: list[Any] = [lo] if hi is None else [lo, hi]
    counts: dict[str, int] = {}

    async def run(name: str, sql: str, *a: Any) -> None:
        status = await conn.execute(sql, *a)
        counts[name] = counts.get(name, 0) + int(status.split()[-1])

    await run(
        "equipment_state",
        "DELETE FROM equipment_state WHERE start_ts >= $1" + hi_cond.format(col="start_ts"),
        *args,
    )
    await run(
        "downtime",
        "DELETE FROM downtime WHERE import_id IS NULL AND start_ts >= $1"
        + hi_cond.format(col="start_ts"),
        *args,
    )
    if reopen:
        await run(
            "equipment_state_reopened",
            "UPDATE equipment_state SET end_ts = NULL WHERE start_ts < $1 AND end_ts > $1",
            lo,
        )
        await run(
            "downtime_reopened",
            "UPDATE downtime SET end_ts = NULL, duration_s = NULL, microstop = false "
            "WHERE import_id IS NULL AND start_ts < $1 AND end_ts > $1",
            lo,
        )
    end = hi if hi is not None else datetime.max.replace(tzinfo=lo.tzinfo)
    keys = [
        (s.shift_date, s.code)
        for s in cfg.calendar.shifts_between(
            lo, min(end, lo.replace(year=lo.year + 2)), working_only=False
        )
        if s.start >= lo
    ]
    if keys:
        dates = [k[0] for k in keys]
        codes = [k[1] for k in keys]
        for table, extra in (("kpi_shift", " AND source = 'events'"), ("bottleneck_shift", "")):
            await run(
                table,
                f"DELETE FROM {table} t USING unnest($1::date[], $2::text[]) AS k(d, c) "
                f"WHERE t.shift_date = k.d AND t.shift_code = k.c{extra}",
                dates,
                codes,
            )
    lo_day: date = cfg.calendar.local_date(lo)
    await run(
        "alert",
        "DELETE FROM alert WHERE ts >= $1"
        + hi_cond.format(col="ts")
        + f" AND split_part(dedup_key, '|', 3) >= ${len(args) + 1}",
        *args,
        lo_day.isoformat(),
    )
    await run(
        "dq_issue",
        "DELETE FROM dq_issue WHERE import_id IS NULL AND ts >= $1" + hi_cond.format(col="ts"),
        *args,
    )
    return counts


FACT_TABLES = ("event_raw", "telemetry", "unit_event", "buffer_level", "ckd_stock", "defect")


async def delete_facts(conn: asyncpg.Connection, lo: datetime) -> dict[str, int]:
    """Delete the collected facts with ``ts >= lo`` (demo reset: the live tail)."""
    counts: dict[str, int] = {}
    for table in FACT_TABLES:
        status = await conn.execute(f"DELETE FROM {table} WHERE ts >= $1", lo)
        counts[table] = int(status.split()[-1])
    return counts
