"""Reclassification of stored stops and recomputation of closed shifts from stored rows
(FR-ENG-03, FR-KPI-04) — for stops older than what the core keeps in memory.

Runs inside the writer's transaction: updates the ``downtime`` rows (the unit and the line stop of
the same instant), then recomputes every working shift the stop overlaps with the same function
as live (:func:`qost_engine.core.engine.line_shift_kpi`) and writes a new ``kpi_shift`` version
with an ``audit_log`` entry.
"""

from __future__ import annotations

import json
from datetime import datetime

import asyncpg
import structlog

from qost_engine.core.effects import ReclassifyRequest
from qost_engine.core.engine import kpi_values, line_shift_kpi
from qost_engine.core.state import LineAcc
from twin_core.config import TwinConfig
from twin_core.events import FIRST_EXIT_RESULTS
from twin_core.kpi import StateSpan, StopSpan, pri_seconds

log = structlog.get_logger("qost_engine.recompute")

_UPDATE = """
UPDATE downtime SET reason_code = $3, reason_source = 'operator', planned = $4,
    comment = COALESCE($5, comment), classified_ts = $6
WHERE import_id IS NULL AND entity = $1 AND start_ts = $2
RETURNING line, start_ts, end_ts
"""


async def reclassify_stored(
    conn: asyncpg.Connection, cfg: TwinConfig, req: ReclassifyRequest
) -> int:
    """Apply one reclassification; returns the number of KPI versions written."""
    rows = await conn.fetch(
        _UPDATE, req.entity, req.start, req.reason_code, req.planned, req.comment, req.ts
    )
    if not rows:
        log.warning("classify_unknown_stop", entity=req.entity, start=req.start.isoformat())
        return 0
    line, start, end = rows[0]["line"], rows[0]["start_ts"], rows[0]["end_ts"]
    if req.entity != line:
        await conn.execute(
            _UPDATE.replace(
                "WHERE import_id IS NULL", "WHERE reason_source = 'auto' AND import_id IS NULL"
            ),
            line,
            req.start,
            req.reason_code,
            req.planned,
            req.comment,
            req.ts,
        )
    shifts = cfg.calendar.shifts_between(start, end or req.ts, working_only=True)
    written = 0
    for sh in shifts:
        if sh.end > req.ts:
            continue  # the running shift is recomputed by the live core
        await _recompute_shift(conn, cfg, line, sh.start, sh.end, sh.shift_date, sh.code, req)
        written += 1
    return written


async def _recompute_shift(
    conn: asyncpg.Connection,
    cfg: TwinConfig,
    line: str,
    lo_dt: datetime,
    hi_dt: datetime,
    shift_date: object,
    code: str,
    req: ReclassifyRequest,
) -> None:
    lo, hi = lo_dt.timestamp(), hi_dt.timestamp()
    spans = [
        StateSpan(r["start_ts"].timestamp(), (r["end_ts"] or hi_dt).timestamp(), r["state"])
        for r in await conn.fetch(
            "SELECT start_ts, end_ts, state FROM equipment_state WHERE entity = $1 "
            "AND start_ts < $3 AND (end_ts IS NULL OR end_ts > $2)",
            line,
            lo_dt,
            hi_dt,
        )
    ]
    stops = [
        StopSpan(
            r["start_ts"].timestamp(),
            (r["end_ts"] or hi_dt).timestamp(),
            r["planned"],
            r["duration_s"]
            if r["duration_s"] is not None
            else (hi_dt - r["start_ts"]).total_seconds(),
        )
        for r in await conn.fetch(
            "SELECT start_ts, end_ts, planned, duration_s FROM downtime WHERE import_id IS NULL "
            "AND entity = $1 AND start_ts < $3 AND (end_ts IS NULL OR end_ts > $2)",
            line,
            lo_dt,
            hi_dt,
        )
    ]
    counts = LineAcc()
    ict = float(cfg.lines[line].ict_seconds)
    for r in await conn.fetch(
        "SELECT product, result, count(*) AS n FROM unit_event WHERE line = $1 "
        "AND ts >= $2 AND ts < $3 GROUP BY product, result",
        line,
        lo_dt,
        hi_dt,
    ):
        if r["result"] not in FIRST_EXIT_RESULTS:
            continue
        product = cfg.products.get(r["product"])
        pri = pri_seconds(ict, product.cycle_factor if product else 1.0)
        n = int(r["n"])
        counts.pq += n
        counts.pri_produced_s += pri * n
        if r["result"] == "pass":
            counts.gq += n
            counts.pri_good_s += pri * n
    kpi = line_shift_kpi(
        window=(lo, hi),
        pot_min=(hi - lo) / 60.0,
        spans=spans,
        stops=stops,
        counts=counts,
        threshold_s=cfg.rules.thresholds.microstop_threshold_s,
    )
    values = kpi_values(kpi)
    version = await conn.fetchval(
        "SELECT COALESCE(max(version), 0) + 1 FROM kpi_shift WHERE line = $1 AND shift_date = $2 "
        "AND shift_code = $3 AND source = 'events'",
        line,
        shift_date,
        code,
    )
    from qost_engine.writer import _kpi_insert

    await _kpi_insert(conn, line, shift_date, code, int(version), values, req.ts)
    await conn.execute(
        "INSERT INTO audit_log (ts, user_id, action, entity_type, entity_id, before, after) "
        "VALUES ($1, NULL, 'kpi_shift.recompute', 'kpi_shift', $2, $3::jsonb, $4::jsonb)",
        req.ts,
        f"{line}/{shift_date}/{code}",
        json.dumps({"version": int(version) - 1}),
        json.dumps(
            {
                "version": int(version),
                "reason": "classify_downtime",
                "oee": values["oee"],
                "by": req.user,
                "source": "engine",
                "stop": {
                    "entity": req.entity,
                    "start_ts": req.start.isoformat(),
                    "reason_code": req.reason_code,
                },
            }
        ),
    )
