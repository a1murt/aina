"""``GET /bottleneck?from=&to=`` — sole/shifting shares and the chronology (§9.5); all roles.

* Event-sourced shifts: ``bottleneck_shift`` (active-period method, written at shift close);
  period shares are the duration-weighted means of the shift shares; the period bottleneck is
  argmax(sole + shifting).
* Imported days (no states): the aggregate path of §9.5 — the line with the minimum PQ of the
  day, ``shifting`` if the daily bottleneck changes over the period.
* ``live``: the current bottleneck and shift shares from the engine's live view (if available).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from redis.asyncio import Redis
from sqlalchemy import text

from qost_api.auth import AnyUser
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, live_keys, parse_day
from qost_api.problems import ProblemError

router = APIRouter(prefix="/api/v1", tags=["bottleneck"])

_SHIFTS = text(
    """
    SELECT shift_date, shift_code, line, sole_share, shifting_share FROM bottleneck_shift
    WHERE line_group = :group AND shift_date BETWEEN :d0 AND :d1
    ORDER BY shift_date, shift_code, line
    """
)
_IMPORT_PQ = text(
    """
    SELECT DISTINCT ON (line, shift_date, shift_code) line, shift_date, shift_code, pq
    FROM kpi_shift WHERE source = 'import' AND shift_date BETWEEN :d0 AND :d1
    ORDER BY line, shift_date, shift_code, version DESC
    """
)


@router.get("/bottleneck", summary="Bottleneck shares (sole/shifting) and chronology")
async def get_bottleneck(
    principal: AnyUser,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    start: Annotated[
        str | None, Query(alias="from", description="plant-local date (default: month start)")
    ] = None,
    end: Annotated[
        str | None, Query(alias="to", description="plant-local date, inclusive (default: today)")
    ] = None,
) -> dict[str, Any]:
    today = clock.now().astimezone(cfg.timezone).date()
    d1 = parse_day(end, cfg) or today
    d0 = parse_day(start, cfg) or d1.replace(day=1)
    if d1 < d0:
        raise ProblemError(
            422, "Request validation failed", "'from' is after 'to'", slug="validation"
        )
    flow = list(cfg.flow_lines)
    rows = (
        await session.execute(_SHIFTS, {"group": cfg.plant.site.code, "d0": d0, "d1": d1})
    ).mappings()
    per_shift: dict[tuple[date, str], dict[str, dict[str, float]]] = defaultdict(dict)
    for r in rows:
        per_shift[(r["shift_date"], r["shift_code"])][r["line"]] = {
            "sole": float(r["sole_share"]),
            "shifting": float(r["shifting_share"]),
        }
    totals: dict[str, list[float]] = {line: [0.0, 0.0] for line in flow}
    chronology: list[dict[str, Any]] = []
    for (day, code), shares in sorted(per_shift.items()):
        leader = max(
            shares,
            key=lambda ln: (
                shares[ln]["sole"] + shares[ln]["shifting"],
                -flow.index(ln) if ln in flow else 0,
            ),
        )
        for line, s in shares.items():
            acc = totals.setdefault(line, [0.0, 0.0])
            acc[0] += s["sole"]
            acc[1] += s["shifting"]
        chronology.append(
            {
                "date": day.isoformat(),
                "shift": code,
                "method": "active_periods",
                "line": leader,
                "shares": shares,
            }
        )
    n = len(per_shift)
    shares_out = (
        {
            line: {"sole": round(v[0] / n, 4), "shifting": round(v[1] / n, 4)}
            for line, v in totals.items()
        }
        if n
        else {}
    )
    overall = (
        max(
            shares_out,
            key=lambda ln: (shares_out[ln]["sole"] + shares_out[ln]["shifting"], -flow.index(ln)),
        )
        if shares_out
        else None
    )
    # aggregate path for imported days (§9.5)
    pq: dict[date, dict[str, int]] = defaultdict(dict)
    for r in (await session.execute(_IMPORT_PQ, {"d0": d0, "d1": d1})).mappings():
        pq[r["shift_date"]][r["line"]] = pq[r["shift_date"]].get(r["line"], 0) + int(r["pq"])
    aggregate_days: list[dict[str, Any]] = []
    for day in sorted(pq):
        lines = pq[day]
        leader = min(lines, key=lambda ln: (lines[ln], flow.index(ln) if ln in flow else 0))
        aggregate_days.append(
            {"date": day.isoformat(), "method": "aggregate", "line": leader, "pq": lines}
        )
    aggregate = None
    if aggregate_days:
        sums: dict[str, list[int]] = defaultdict(list)
        for d in aggregate_days:
            day_pq: dict[str, int] = d["pq"]
            for line, q in day_pq.items():
                sums[line].append(q)
        means = {line: sum(v) / len(v) for line, v in sums.items()}
        aggregate = {
            "line": min(means, key=lambda ln: (means[ln], flow.index(ln) if ln in flow else 0)),
            "shifting": len({d["line"] for d in aggregate_days}) > 1,
            "days": aggregate_days,
        }
    live = None
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            raw = await redis.get(live_keys(request.app.state.settings).key("bottleneck"))
        except (OSError, ConnectionError):
            raw = None
        if raw:
            import json

            live = json.loads(raw)
    return {
        "from": d0.isoformat(),
        "to": d1.isoformat(),
        "line_group": cfg.plant.site.code,
        "shifts": n,
        "shares": shares_out,
        "overall": overall,
        "chronology": chronology,
        "aggregate": aggregate,
        "live": live,
    }
