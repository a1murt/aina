"""Database access for the forecast (SPEC §8, §10.1): calibration facts, plant state, targets,
calibration snapshots and forecast runs.

Reads the facts the engine writes (``equipment_state``, ``downtime``, ``unit_event``,
``buffer_level``, ``telemetry``, ``ckd_stock``, ``production_plan``) and turns them into the pure
inputs of :mod:`twin_core.forecast`. Aggregation happens in SQL; shift windows are passed as
arrays (the ``shift`` table is materialised later, FR-DB-01).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any, Protocol

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qost_api.audit import audit
from qost_api.auth import Principal
from qost_api.queries.plan import mtd_output
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.db import CalibrationSnapshot, ForecastRun, ProductionPlan
from twin_core.domain import EquipmentState
from twin_core.events import FIRST_EXIT_RESULTS
from twin_core.forecast.calibration import (
    WIP_LOOKBACK,
    CalibrationInputs,
    LineShiftFacts,
    Stop,
    UnitExit,
    held_from_exits,
    targets_from_config,
)
from twin_core.forecast.params import CalibrationParams, KitLot, OpenDown, PlantState, Targets
from twin_core.schedule import ckd_lot_sizes, is_delivery_day, schedule_anchor, working_day_index

_DOWN = (EquipmentState.DOWN_UNPLANNED.value, EquipmentState.DOWN_PLANNED.value)
_REJECTS = ("defect", "scrap")
FILTER_DP_MAX_AGE = timedelta(hours=6)
"""Older filter pressure-drop samples are not trusted as the current value."""


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    id: int
    ts: datetime
    window_days: int
    params: CalibrationParams


@dataclass(frozen=True, slots=True)
class RunRecord:
    created_ts: datetime
    mode: str
    month: str
    overrides: dict[str, Any]
    n_runs: int
    seed: int
    status: str
    progress: float
    result: dict[str, Any] | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class StoredRun:
    id: int
    created_by: int | None
    record: RunRecord


class ForecastBackend(Protocol):
    """Data access of the forecast service (database, or fixtures in tests)."""

    async def calibration_inputs(
        self,
        cfg: TwinConfig,
        *,
        window_from: datetime,
        window_to: datetime,
        working_days: int,
        as_of: datetime,
    ) -> CalibrationInputs: ...

    async def plant_state(self, cfg: TwinConfig, *, as_of: datetime) -> PlantState: ...

    async def targets(self, cfg: TwinConfig, month: str) -> Targets: ...

    async def find_snapshot(
        self, *, window_to: datetime, window_days: int, config_hash: str
    ) -> StoredSnapshot | None: ...

    async def save_snapshot(
        self, params: CalibrationParams, *, ts: datetime, principal: Principal
    ) -> StoredSnapshot: ...

    async def save_run(self, record: RunRecord, *, principal: Principal) -> int: ...

    async def get_run(self, run_id: int) -> StoredRun | None: ...


def snapshot_key(window_to: datetime, window_days: int, config_hash: str) -> str:
    return f"calibration|{ensure_utc(window_to).isoformat()}|{window_days}|{config_hash}"


# --------------------------------------------------------------------------- SQL

_OPERATING_SQL = text(
    """
    SELECT entity,
           SUM(EXTRACT(EPOCH FROM LEAST(COALESCE(end_ts, :as_of), :w1) - GREATEST(start_ts, :w0)))
             AS seconds
    FROM equipment_state
    WHERE entity_type = 'equipment' AND state = 'RUNNING' AND entity = ANY(:codes)
      AND start_ts < :w1 AND COALESCE(end_ts, :as_of) > :w0
    GROUP BY entity
    """
)

_STOPS_SQL = text(
    """
    SELECT entity, start_ts, end_ts, planned, reason_code
    FROM downtime
    WHERE microstop = false AND entity = ANY(:codes)
      AND start_ts >= :w0 AND start_ts < :w1 AND start_ts <= :as_of
    ORDER BY entity, start_ts
    """
)

_SHIFTS_CTE = """
    WITH s AS (
      SELECT t.s_start, t.s_end, t.idx
      FROM unnest(CAST(:starts AS timestamptz[]), CAST(:ends AS timestamptz[]))
           WITH ORDINALITY AS t(s_start, s_end, idx)
    )
"""

_LINE_TIME_SQL = text(
    _SHIFTS_CTE
    + """
    SELECT e.entity, s.idx, e.state,
           SUM(EXTRACT(EPOCH FROM LEAST(COALESCE(e.end_ts, :as_of), s.s_end)
                                  - GREATEST(e.start_ts, s.s_start))) AS seconds
    FROM equipment_state e
    JOIN s ON e.start_ts < s.s_end AND COALESCE(e.end_ts, :as_of) > s.s_start
    WHERE e.entity_type = 'line' AND e.entity = ANY(:lines)
      AND e.start_ts < :w1 AND COALESCE(e.end_ts, :as_of) > :w0
      AND (e.state IN ('RUNNING', 'DEGRADED')
           OR (e.state = 'DOWN_UNPLANNED' AND e.end_ts IS NOT NULL
               AND e.end_ts - e.start_ts < make_interval(secs => :threshold)))
    GROUP BY e.entity, s.idx, e.state
    """
)

_EXITS_SQL = text(
    _SHIFTS_CTE
    + """
    SELECT u.line, s.idx, u.product, u.result, u.defect_code, COUNT(*) AS n
    FROM unit_event u
    JOIN s ON u.ts >= s.s_start AND u.ts < s.s_end
    WHERE u.line = ANY(:lines) AND u.result = ANY(:results) AND u.ts >= :w0 AND u.ts < :w1
    GROUP BY u.line, s.idx, u.product, u.result, u.defect_code
    """
)

_EXITS_RECENT_SQL = text(
    """
    SELECT body_id, line, ts, result FROM unit_event
    WHERE line = ANY(:lines) AND ts > :since AND ts <= :as_of
    """
)

_BUFFERS_SQL = text(
    """
    SELECT DISTINCT ON (buffer) buffer, level
    FROM buffer_level WHERE buffer = ANY(:codes) AND ts <= :as_of
    ORDER BY buffer, ts DESC
    """
)

_LAST_STATE_SQL = text(
    """
    SELECT DISTINCT ON (entity) entity, state, reason_code, start_ts, end_ts
    FROM equipment_state
    WHERE entity = ANY(:codes) AND entity_type = 'equipment' AND start_ts <= :as_of
    ORDER BY entity, start_ts DESC
    """
)

_FILTER_DP_SQL = text(
    """
    SELECT DISTINCT ON (equipment) equipment, value
    FROM telemetry
    WHERE equipment = ANY(:codes) AND signal = :signal AND ts <= :as_of AND ts > :since
    ORDER BY equipment, ts DESC
    """
)

_KITS_SQL = text(
    """
    SELECT DISTINCT ON (product) product, kits
    FROM ckd_stock WHERE product = ANY(:products) AND ts <= :as_of
    ORDER BY product, ts DESC
    """
)

_KITS_HISTORY_SQL = text(
    """
    SELECT product, ts, kits FROM ckd_stock
    WHERE product = ANY(:products) AND ts > :since AND ts <= :as_of
    ORDER BY product, ts
    """
)


def _shift_windows(
    cfg: TwinConfig, w0: datetime, w1: datetime
) -> list[tuple[datetime, datetime, date, str]]:
    shifts = cfg.calendar.shifts_between(w0, w1, working_only=True)
    return [(s.start, s.end, s.shift_date, s.code) for s in shifts if s.start >= w0 and s.end <= w1]


async def load_calibration_inputs(
    session: AsyncSession,
    cfg: TwinConfig,
    *,
    window_from: datetime,
    window_to: datetime,
    working_days: int,
    as_of: datetime,
) -> CalibrationInputs:
    w0, w1, now = ensure_utc(window_from), ensure_utc(window_to), ensure_utc(as_of)
    codes = list(cfg.equipment)
    lines = list(cfg.flow_lines)
    rows = await session.execute(_OPERATING_SQL, {"w0": w0, "w1": w1, "as_of": now, "codes": codes})
    operating = {r.entity: float(r.seconds or 0.0) / 3600.0 for r in rows}
    stops = [
        Stop(
            equipment=r.entity,
            start=r.start_ts,
            end=r.end_ts,
            planned=bool(r.planned),
            reason=r.reason_code,
        )
        for r in await session.execute(
            _STOPS_SQL, {"w0": w0, "w1": w1, "as_of": now, "codes": codes}
        )
    ]
    windows = _shift_windows(cfg, w0, w1)
    params = {
        "starts": [w[0] for w in windows],
        "ends": [w[1] for w in windows],
        "w0": w0,
        "w1": w1,
        "as_of": now,
        "lines": lines,
    }
    apt: dict[tuple[str, int], float] = defaultdict(float)
    degraded: dict[tuple[str, int], float] = defaultdict(float)
    if windows:
        threshold = cfg.rules.thresholds.microstop_threshold_s
        for r in await session.execute(_LINE_TIME_SQL, {**params, "threshold": threshold}):
            key = (r.entity, int(r.idx) - 1)
            apt[key] += float(r.seconds or 0.0)
            if r.state == EquipmentState.DEGRADED.value:
                degraded[key] += float(r.seconds or 0.0)
    exits: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    defects: Counter[tuple[str, int]] = Counter()
    repaint: Counter[tuple[str, int]] = Counter()
    by_code: dict[str, Counter[str]] = defaultdict(Counter)
    if windows:
        results = sorted(FIRST_EXIT_RESULTS)
        for r in await session.execute(_EXITS_SQL, {**params, "results": results}):
            key = (r.line, int(r.idx) - 1)
            n = int(r.n)
            exits[key][r.product] += n
            if r.result in _REJECTS:
                defects[key] += n
                if r.defect_code is not None:
                    by_code[r.line][r.defect_code] += n
                    known = cfg.defects.get(r.defect_code)
                    if known is not None and known.repaint:
                        repaint[key] += n
    facts = [
        LineShiftFacts(
            line=line,
            shift_date=window[2],
            shift_code=window[3],
            apt_s=apt.get((line, i), 0.0),
            degraded_s=degraded.get((line, i), 0.0),
            exits=dict(exits.get((line, i), {})),
            defects=defects.get((line, i), 0),
            repaint_defects=repaint.get((line, i), 0),
        )
        for i, window in enumerate(windows)
        for line in lines
    ]
    return CalibrationInputs(
        window_from=w0,
        window_to=w1,
        working_days=working_days,
        operating_h={c: operating.get(c, 0.0) for c in codes},
        stops=tuple(stops),
        line_shifts=tuple(facts),
        defect_codes={line: dict(c) for line, c in by_code.items()},
    )


def _scheduled_dispatches(
    cfg: TwinConfig, since: datetime, until: datetime
) -> list[tuple[str, int, datetime]]:
    """CKD lots dispatched in ``(since, until]`` by the configured schedule."""
    cal = cfg.calendar
    anchor = schedule_anchor(cfg)
    tz = cfg.timezone
    day = ensure_utc(since).astimezone(tz).date()
    last = ensure_utc(until).astimezone(tz).date()
    out: list[tuple[str, int, datetime]] = []
    while day <= last:
        shifts = cal.shifts_on(day, working_only=True)
        if shifts and is_delivery_day(cfg, working_day_index(cal, anchor, day)):
            t = shifts[0].start
            if ensure_utc(since) < t <= ensure_utc(until):
                out.extend((p, q, t) for p, q in ckd_lot_sizes(cfg, day).items() if q > 0)
        day += timedelta(days=1)
    return out


async def load_plant_state(
    session: AsyncSession, cfg: TwinConfig, *, as_of: datetime
) -> PlantState:
    now = ensure_utc(as_of)
    warnings: list[str] = []
    mtd = await mtd_output(session, cfg, as_of=now)
    buffers = {
        r.buffer: float(r.level)
        for r in await session.execute(_BUFFERS_SQL, {"codes": list(cfg.buffers), "as_of": now})
    }
    for code in cfg.buffers:
        if code not in buffers:
            warnings.append(f"no level of buffer {code}: initial level from the configuration")
            buffers[code] = float(cfg.simulation.process.initial_buffers.get(code, 0))
    exits = [
        UnitExit(r.body_id, r.line, r.ts, r.result)
        for r in await session.execute(
            _EXITS_RECENT_SQL,
            {"lines": list(cfg.flow_lines), "since": now - WIP_LOOKBACK, "as_of": now},
        )
    ]
    held = held_from_exits(cfg, exits, buffers)
    open_downs: list[OpenDown] = []
    for r in await session.execute(_LAST_STATE_SQL, {"codes": list(cfg.equipment), "as_of": now}):
        if r.state in _DOWN and (r.end_ts is None or r.end_ts > now):
            open_downs.append(
                OpenDown(equipment=r.entity, state=r.state, reason=r.reason_code, since=r.start_ts)
            )
    filter_dp: dict[str, float] = {}
    pf = cfg.simulation.paint_filters
    if pf is not None:
        booths = [c for c, eq in cfg.equipment.items() if eq.type == pf.equipment_type]
        rows = await session.execute(
            _FILTER_DP_SQL,
            {"codes": booths, "signal": pf.signal, "as_of": now, "since": now - FILTER_DP_MAX_AGE},
        )
        filter_dp = {r.equipment: float(r.value) for r in rows}
        for code in booths:
            if code not in filter_dp:
                warnings.append(f"no recent {pf.signal} of {code}: filter state drawn at random")
    products = list(cfg.products)
    kits = {
        r.product: float(r.kits)
        for r in await session.execute(_KITS_SQL, {"products": products, "as_of": now})
    }
    supply = cfg.simulation.ckd_supply
    for p in products:
        if p not in kits:
            kits[p] = float(supply.initial_kits.get(p, 0))
    # lots dispatched by the schedule that have not arrived yet (no matching stock jump)
    since = now - timedelta(days=supply.delay_days.max)
    dispatches = _scheduled_dispatches(cfg, since, now)
    transit: list[KitLot] = []
    if dispatches:
        history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for r in await session.execute(
            _KITS_HISTORY_SQL, {"products": products, "since": since, "as_of": now}
        ):
            history[r.product].append((r.ts, float(r.kits)))
        for product in products:
            lots = sorted((t, q) for p, q, t in dispatches if p == product)
            points = history.get(product, [])
            jumps = [(t1, k1 - k0) for (_t0, k0), (t1, k1) in pairwise(points) if k1 > k0]
            for t, qty in lots:
                match = next((j for j in jumps if j[0] >= t and j[1] >= qty / 2), None)
                if match is not None:
                    jumps.remove(match)
                    continue
                transit.append(KitLot(product=product, qty=qty, dispatched=t))
    return PlantState(
        as_of=now,
        mtd_output=mtd,
        buffers=buffers,
        open_downs=open_downs,
        filter_dp=filter_dp,
        kits=kits,
        kits_in_transit=transit,
        held=held,
        source="db",
        warnings=warnings,
    )


async def load_targets(session: AsyncSession, cfg: TwinConfig, month: str) -> Targets:
    rows = (
        await session.execute(select(ProductionPlan).where(ProductionPlan.month == month))
    ).scalars()
    plant: int | None = None
    line_total = 0
    has_line = False
    for row in rows:
        if row.level == "plant_target":
            plant = row.qty
        elif row.level == "line_model":
            line_total += row.qty
            has_line = True
    fallback = targets_from_config(cfg, month)
    if plant is None and not has_line:
        return fallback
    return Targets(
        plant_target=plant if plant is not None else fallback.plant_target,
        line_plan=line_total if has_line else fallback.line_plan,
        source="db",
    )


class DbForecastBackend:
    """:class:`ForecastBackend` over the application database."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessionmaker

    async def calibration_inputs(
        self,
        cfg: TwinConfig,
        *,
        window_from: datetime,
        window_to: datetime,
        working_days: int,
        as_of: datetime,
    ) -> CalibrationInputs:
        async with self._sessions() as session:
            return await load_calibration_inputs(
                session,
                cfg,
                window_from=window_from,
                window_to=window_to,
                working_days=working_days,
                as_of=as_of,
            )

    async def plant_state(self, cfg: TwinConfig, *, as_of: datetime) -> PlantState:
        async with self._sessions() as session:
            return await load_plant_state(session, cfg, as_of=as_of)

    async def targets(self, cfg: TwinConfig, month: str) -> Targets:
        async with self._sessions() as session:
            return await load_targets(session, cfg, month)

    async def find_snapshot(
        self, *, window_to: datetime, window_days: int, config_hash: str
    ) -> StoredSnapshot | None:
        async with self._sessions() as session:
            return await _find_snapshot(session, window_to, window_days, config_hash)

    async def save_snapshot(
        self, params: CalibrationParams, *, ts: datetime, principal: Principal
    ) -> StoredSnapshot:
        key = snapshot_key(params.window_to, params.working_days, params.config_hash)
        async with self._sessions() as session, session.begin():
            # one snapshot per key, also under concurrent requests
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
            existing = await _find_snapshot(
                session, params.window_to, params.working_days, params.config_hash
            )
            if existing is not None:
                return existing
            row = CalibrationSnapshot(
                ts=ts, window_days=params.working_days, params=params.model_dump(mode="json")
            )
            session.add(row)
            await session.flush()
            audit(
                session,
                ts=ts,
                principal=principal,
                action="calibration.create",
                entity_type="calibration_snapshot",
                entity_id=str(row.id),
                after={
                    "window_from": params.window_from.isoformat(),
                    "window_to": params.window_to.isoformat(),
                    "working_days": params.working_days,
                    "config_hash": params.config_hash,
                },
            )
            return StoredSnapshot(row.id, ts, row.window_days, params)

    async def save_run(self, record: RunRecord, *, principal: Principal) -> int:
        async with self._sessions() as session, session.begin():
            row = ForecastRun(
                created_ts=record.created_ts,
                created_by=principal.user_id,
                mode=record.mode,
                month=record.month,
                overrides=record.overrides,
                n_runs=record.n_runs,
                seed=record.seed,
                status=record.status,
                progress=record.progress,
                result=record.result,
                duration_ms=record.duration_ms,
            )
            session.add(row)
            await session.flush()
            audit(
                session,
                ts=record.created_ts,
                principal=principal,
                action="forecast.run",
                entity_type="forecast_run",
                entity_id=str(row.id),
                after={
                    "mode": record.mode,
                    "month": record.month,
                    "n_runs": record.n_runs,
                    "seed": record.seed,
                    "overrides": record.overrides,
                },
            )
            return int(row.id)

    async def get_run(self, run_id: int) -> StoredRun | None:
        async with self._sessions() as session:
            row = await session.get(ForecastRun, run_id)
            if row is None:
                return None
            return StoredRun(
                id=row.id,
                created_by=row.created_by,
                record=RunRecord(
                    created_ts=row.created_ts,
                    mode=row.mode,
                    month=row.month,
                    overrides=dict(row.overrides or {}),
                    n_runs=row.n_runs,
                    seed=row.seed,
                    status=row.status,
                    progress=row.progress,
                    result=row.result,
                    duration_ms=row.duration_ms,
                ),
            )


async def _find_snapshot(
    session: AsyncSession, window_to: datetime, window_days: int, config_hash: str
) -> StoredSnapshot | None:
    probe = _window_to_json(window_to)
    row = (
        await session.execute(
            select(CalibrationSnapshot)
            .where(
                CalibrationSnapshot.window_days == window_days,
                CalibrationSnapshot.params["window_to"].astext == probe,
                CalibrationSnapshot.params["config_hash"].astext == config_hash,
            )
            .order_by(CalibrationSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return StoredSnapshot(
        row.id, row.ts, row.window_days, CalibrationParams.model_validate(row.params)
    )


_DATETIME = TypeAdapter(datetime)


def _window_to_json(window_to: datetime) -> str:
    """``window_to`` exactly as :class:`CalibrationParams` serialises it to JSON."""
    return str(_DATETIME.dump_python(ensure_utc(window_to), mode="json"))
