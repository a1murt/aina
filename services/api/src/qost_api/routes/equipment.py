"""``GET /equipment/{code}/health`` and ``/equipment/{code}/telemetry`` — all roles (§12.2).

* health: live state (engine view), latest PdM prediction (``prediction``; ``null`` until the
  engine serves models, M7b), last value of every signal with its threshold status, MTBF/MTTR
  over the last 30 days of working shifts, the open stop.
* telemetry: ``agg=raw`` (``telemetry``), ``15m`` / ``1h`` (continuous aggregates
  ``telemetry_15m`` / ``telemetry_1h``: avg, min, max, last, n) or ``auto`` (raw ≤ 6 h,
  15 min ≤ 3 days, else 1 h). Points are ``[ts, avg, min, max]`` (raw: value three times).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from redis.asyncio import Redis
from sqlalchemy import text

from qost_api.auth import AnyUser
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, live_keys, window
from qost_api.problems import ProblemError
from qost_api.queries.kpi import equipment_rows
from twin_core.config import Signal, TwinConfig

router = APIRouter(prefix="/api/v1/equipment", tags=["equipment"])

_LAST = text(
    """
    SELECT DISTINCT ON (signal) signal, ts, value, quality FROM telemetry
    WHERE equipment = :code AND ts > :since AND ts <= :now
    ORDER BY signal, ts DESC
    """
)
_PREDICTION = text(
    """
    SELECT ts, horizon_h, p_failure, health_index, model_version, top_factors FROM prediction
    WHERE equipment = :code AND ts <= :now ORDER BY ts DESC LIMIT 1
    """
)
_SERIES = {
    "raw": "SELECT ts AS t, value AS avg, value AS min, value AS max FROM telemetry "
    "WHERE equipment = :code AND signal = :signal AND ts >= :lo AND ts < :hi "
    "ORDER BY ts LIMIT :cap",
    "15m": "SELECT bucket AS t, avg, min, max FROM telemetry_15m "
    "WHERE equipment = :code AND signal = :signal AND bucket >= :lo AND bucket < :hi "
    "ORDER BY bucket LIMIT :cap",
    "1h": "SELECT bucket AS t, avg, min, max FROM telemetry_1h "
    "WHERE equipment = :code AND signal = :signal AND bucket >= :lo AND bucket < :hi "
    "ORDER BY bucket LIMIT :cap",
}
MAX_POINTS = 5000


def _equipment(cfg: TwinConfig, code: str) -> Any:
    eq = cfg.equipment.get(code)
    if eq is None:
        raise ProblemError(404, "Not Found", f"unknown equipment '{code}'", slug="not-found")
    return eq


def signal_status(signal: Signal, value: float | None) -> str | None:
    """``normal`` | ``warning`` (outside warn_lo..warn_hi) | ``limit`` (beyond limit_*)."""
    if value is None:
        return None
    if (signal.limit_hi is not None and value >= signal.limit_hi) or (
        signal.limit_lo is not None and value <= signal.limit_lo
    ):
        return "limit"
    if (signal.warn_hi is not None and value >= signal.warn_hi) or (
        signal.warn_lo is not None and value <= signal.warn_lo
    ):
        return "warning"
    return "normal"


@router.get("/{code}/health", summary="Health of a unit: state, prediction, signals, MTBF/MTTR")
async def get_health(
    code: str,
    principal: AnyUser,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    session: Session,
) -> dict[str, Any]:
    eq = _equipment(cfg, code)
    now = clock.now()
    signals = cfg.equipment_types[eq.type].signals
    last = {
        r["signal"]: r
        for r in (
            await session.execute(
                _LAST, {"code": code, "since": now - timedelta(hours=6), "now": now}
            )
        ).mappings()
    }
    pred = (await session.execute(_PREDICTION, {"code": code, "now": now})).mappings().first()
    day = now.astimezone(cfg.timezone).date()
    shifts = [s for s in cfg.calendar.materialize(day - timedelta(days=30), day) if s.working]
    stats = await equipment_rows(session, cfg, [code], shifts, now, "month")
    total_pot = sum(float(r["pot"]) for r in stats)
    failures = sum(int(r["failures"]) for r in stats)
    repair = sum(float(r["repair_min"]) for r in stats)
    run_h = sum(float(r["apt"]) for r in stats) / 60.0
    live: dict[str, Any] | None = None
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            raw = await redis.hget(live_keys(request.app.state.settings).key("equipment"), code)
            live = json.loads(raw) if raw else None
        except (OSError, ConnectionError):
            live = None
    line = cfg.line_of_equipment(code)
    return {
        "code": code,
        "name_ru": eq.name_ru,
        "type": eq.type,
        "criticality": eq.criticality,
        "line": line.code,
        "area": cfg.area_of_equipment(code).code,
        "state": (live or {}).get("state"),
        "since": (live or {}).get("since"),
        "reason_code": (live or {}).get("reason_code"),
        "alarm": bool((live or {}).get("alarm")),
        "downtime": (live or {}).get("downtime"),
        "health_index": pred["health_index"] if pred else (live or {}).get("health_index"),
        "prediction": None
        if pred is None
        else {
            "ts": pred["ts"].isoformat(),
            "horizon_h": pred["horizon_h"],
            "p_failure": pred["p_failure"],
            "model_version": pred["model_version"],
            "top_factors": pred["top_factors"],
        },
        "signals": [
            {
                **s.model_dump(mode="json"),
                "value": last[s.code]["value"] if s.code in last else None,
                "ts": last[s.code]["ts"].isoformat() if s.code in last else None,
                "quality": last[s.code]["quality"] if s.code in last else None,
                "status": signal_status(s, last[s.code]["value"] if s.code in last else None),
            }
            for s in signals
        ],
        "reliability_30d": {
            "pot_min": round(total_pot, 1),
            "failures": failures,
            "repair_min": round(repair, 1),
            "mtbf_h": round(run_h / failures, 2) if failures else None,
            "mttr_min": round(repair / failures, 1) if failures else None,
        },
    }


@router.get("/{code}/telemetry", summary="Signal history (raw or continuous aggregates)")
async def get_telemetry(
    code: str,
    principal: AnyUser,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    signal: Annotated[
        list[str] | None, Query(description="signal codes; default: all of the type")
    ] = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    agg: Annotated[Literal["auto", "raw", "15m", "1h"], Query()] = "auto",
) -> dict[str, Any]:
    eq = _equipment(cfg, code)
    defs = {s.code: s for s in cfg.equipment_types[eq.type].signals}
    wanted = [c.strip() for item in (signal or []) for c in item.split(",") if c.strip()] or list(
        defs
    )
    unknown = [c for c in wanted if c not in defs]
    if unknown:
        raise ProblemError(
            404,
            "Not Found",
            f"unknown signal(s) {', '.join(unknown)} of {code} (known: {', '.join(defs)})",
            slug="not-found",
        )
    lo, hi = window(
        cfg, clock, start, end, default=timedelta(hours=24), max_span=timedelta(days=31)
    )
    span = hi - lo
    chosen = agg
    if agg == "auto":
        chosen = (
            "raw" if span <= timedelta(hours=6) else "15m" if span <= timedelta(days=3) else "1h"
        )
    sql = text(_SERIES[chosen])
    series = []
    for name in wanted:
        rows = await session.execute(
            sql, {"code": code, "signal": name, "lo": lo, "hi": hi, "cap": MAX_POINTS}
        )
        points = [[r[0].isoformat(), r[1], r[2], r[3]] for r in rows.all()]
        series.append({**defs[name].model_dump(mode="json"), "points": points})
    return {
        "code": code,
        "from": lo.isoformat(),
        "to": hi.isoformat(),
        "agg": chosen,
        "columns": ["ts", "avg", "min", "max"],
        "signals": series,
    }
