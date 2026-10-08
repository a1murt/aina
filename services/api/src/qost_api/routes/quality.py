"""``/quality/spc``, ``/quality/pareto``, ``/quality/correlations`` — roles quality, director,
master (SPEC §11.3, §11.4, §12.2).

* ``spc``: p-chart per production area over closed shifts (``kpi_shift``: imports win over events,
  like every KPI), Western Electric rules 1–4. p̄ is the mean of the last 20 shifts of the series,
  so the series includes 14 days before ``from`` as context. Shifts flagged as a known special
  cause in ``settings.spc_special_causes`` (``{"PAINT": ["2026-10-14/B"]}``) stay on the chart but
  leave p̄.
* ``pareto``: defects by code over the period with the cumulative share and the "vital few"
  covering 80%.
* ``correlations``: Spearman ρ between the hourly defect rate of an area and the process factors
  of its paint booths (filter pressure drop, humidity deviation from the middle of its band).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, local_day_bounds, parse_day, window
from qost_api.problems import ProblemError
from qost_api.queries.kpi import line_shifts
from twin_core.alert_text import SPC_RULES_RU, num
from twin_core.config import TwinConfig
from twin_core.correlations import HourStat, band_center, defect_correlations
from twin_core.events import FIRST_EXIT_RESULTS
from twin_core.spc import BASELINE_SHIFTS, Subgroup, p_chart

router = APIRouter(prefix="/api/v1/quality", tags=["quality"])
Quality = Annotated[Principal, Depends(require_roles("quality", "director", "master"))]
CONTEXT_DAYS = 14
MAX_SCATTER = 400
PARETO_SHARE = 0.8
DEFECT_RESULTS = ("defect", "scrap")


def production_areas(cfg: TwinConfig) -> dict[str, list[str]]:
    return {a.code: [ln.code for ln in a.lines] for a in cfg.plant.areas if a.lines}


def _area_or_404(cfg: TwinConfig, area: str | None) -> list[str]:
    areas = production_areas(cfg)
    if area is None:
        return list(areas)
    if area not in areas:
        raise ProblemError(
            404,
            "Not Found",
            f"unknown area '{area}' (known: {', '.join(areas)})",
            slug="not-found",
        )
    return [area]


def _period(cfg: TwinConfig, clock: Any, start: str | None, end: str | None) -> tuple[date, date]:
    today = clock.now().astimezone(cfg.timezone).date()
    d1 = parse_day(end, cfg) or today
    d0 = parse_day(start, cfg) or d1 - timedelta(days=13)
    if d1 < d0:
        raise ProblemError(
            422, "Request validation failed", "'from' is after 'to'", slug="validation"
        )
    if (d1 - d0).days > 400:
        raise ProblemError(
            422, "Request validation failed", "the period exceeds 400 days", slug="validation"
        )
    return d0, d1


async def special_causes(session: Any) -> dict[str, set[str]]:
    row = (
        await session.execute(text("SELECT value FROM settings WHERE key = 'spc_special_causes'"))
    ).first()
    value = row[0] if row else None
    if not isinstance(value, dict):
        return {}
    return {str(k): {str(x) for x in v} for k, v in value.items() if isinstance(v, list)}


# --------------------------------------------------------------------------- SPC


@router.get("/spc", summary="p-chart per area with Western Electric rules (SPEC §11.3)")
async def get_spc(
    principal: Quality,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    area: Annotated[
        str | None, Query(description="area code; default: every production area")
    ] = None,
    start: Annotated[str | None, Query(alias="from", description="plant-local date")] = None,
    end: Annotated[str | None, Query(alias="to", description="plant-local date, inclusive")] = None,
) -> dict[str, Any]:
    codes = _area_or_404(cfg, area)
    d0, d1 = _period(cfg, clock, start, end)
    areas = production_areas(cfg)
    lines = sorted({ln for a in codes for ln in areas[a]})
    records = [
        r
        for r in await line_shifts(session, lines, d0 - timedelta(days=CONTEXT_DAYS), d1)
        if r.final
    ]
    flagged = await special_causes(session)
    out = []
    for code in codes:
        per_shift: dict[tuple[date, str], list[int]] = defaultdict(lambda: [0, 0])
        for r in records:
            if r.line in areas[code]:
                acc = per_shift[(r.day, r.shift)]
                acc[0] += r.kpi.pq - r.kpi.gq
                acc[1] += r.kpi.pq
        order = {s: i for i, s in enumerate(cfg.calendar.shift_codes)}
        series = sorted(per_shift, key=lambda k: (k[0], order.get(k[1], 99)))
        groups = [
            Subgroup(
                f"{d.isoformat()}/{s}",
                per_shift[(d, s)][0],
                per_shift[(d, s)][1],
                f"{d.isoformat()}/{s}" in flagged.get(code, set()),
            )
            for d, s in series
        ]
        chart = p_chart(groups, norm=cfg.rules.thresholds.defect_rate_limit)
        points = []
        for pt in chart.points:
            day = date.fromisoformat(pt.key.split("/")[0])
            if d0 <= day <= d1:
                points.append(
                    {
                        "key": pt.key,
                        "date": day.isoformat(),
                        "shift": pt.key.split("/")[1],
                        "n": pt.n,
                        "defects": pt.defects,
                        "p": round(pt.p, 5),
                        "ucl": round(pt.ucl, 5),
                        "lcl": round(pt.lcl, 5),
                        "z": round(pt.z, 3),
                        "in_baseline": pt.in_baseline,
                        "special_cause": pt.special_cause,
                        "rules": list(pt.rules),
                    }
                )
        shown = {p["key"] for p in points}
        violations = [
            {
                "rule": v.rule,
                "side": v.side,
                "keys": [k for k in v.keys if k in shown],
                "end_key": v.keys[-1],
                "text_ru": SPC_RULES_RU.get(v.rule, ""),
            }
            for v in chart.violations
            if v.keys[-1] in shown
        ]
        out.append(
            {
                "area": code,
                "name_ru": cfg.areas[code].name_ru,
                "p_bar": None if chart.p_bar is None else round(chart.p_bar, 5),
                "norm": cfg.rules.thresholds.defect_rate_limit,
                "baseline_shifts": len(chart.baseline_keys),
                "baseline_size": BASELINE_SHIFTS,
                "in_control": not violations,
                "points": points,
                "violations": violations,
            }
        )
    return {"from": d0.isoformat(), "to": d1.isoformat(), "areas": out}


# --------------------------------------------------------------------------- Pareto


@router.get("/pareto", summary="Pareto of defect codes with the cumulative share")
async def get_pareto(
    principal: Quality,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    area: str | None = None,
    line: str | None = None,
    start: Annotated[str | None, Query(alias="from", description="plant-local date")] = None,
    end: Annotated[str | None, Query(alias="to", description="plant-local date, inclusive")] = None,
) -> dict[str, Any]:
    if area is not None:
        _area_or_404(cfg, area)
    if line is not None and line not in cfg.lines:
        raise ProblemError(404, "Not Found", f"unknown line '{line}'", slug="not-found")
    today = clock.now().astimezone(cfg.timezone).date()
    d1 = parse_day(end, cfg) or today
    d0 = parse_day(start, cfg) or d1 - timedelta(days=6)
    if d1 < d0:
        raise ProblemError(
            422, "Request validation failed", "'from' is after 'to'", slug="validation"
        )
    lo, hi = local_day_bounds(cfg, d0, d1)
    where = ["ts >= :lo", "ts < :hi"]
    params: dict[str, Any] = {"lo": lo, "hi": hi}
    for name, value in (("area", area), ("line", line)):
        if value:
            where.append(f"{name} = :{name}")
            params[name] = value
    rows = (
        (
            await session.execute(
                text(
                    "SELECT defect_code, area, sum(qty) AS qty, count(*) AS records FROM defect "
                    f"WHERE {' AND '.join(where)} GROUP BY defect_code, area ORDER BY sum(qty) DESC"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    by_code: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = by_code.setdefault(
            r["defect_code"], {"defect_code": r["defect_code"], "qty": 0, "area": r["area"]}
        )
        item["qty"] += int(r["qty"])
    total = sum(i["qty"] for i in by_code.values())
    items = sorted(by_code.values(), key=lambda i: (-i["qty"], i["defect_code"]))
    cumulative = 0
    vital: list[str] = []
    for item in items:
        cumulative += item["qty"]
        code = cfg.defects.get(item["defect_code"])
        item["name_ru"] = code.name_ru if code else None
        item["share"] = round(item["qty"] / total, 4) if total else 0.0
        item["cumulative"] = round(cumulative / total, 4) if total else 0.0
        if total and (not vital or (cumulative - item["qty"]) / total < PARETO_SHARE):
            vital.append(item["defect_code"])
    by_area: dict[str, int] = defaultdict(int)
    for r in rows:
        by_area[r["area"]] += int(r["qty"])
    return {
        "from": d0.isoformat(),
        "to": d1.isoformat(),
        "area": area,
        "line": line,
        "total": total,
        "items": items,
        "vital_few": vital,
        "by_area": [
            {"area": a, "name_ru": cfg.areas[a].name_ru if a in cfg.areas else a, "qty": q}
            for a, q in sorted(by_area.items(), key=lambda kv: -kv[1])
        ],
    }


# --------------------------------------------------------------------------- correlations


def correlation_factors(cfg: TwinConfig, area: str) -> list[dict[str, Any]]:
    """Process factors of an area from the config: the paint booths' filter pressure drop and
    the humidity deviation from the middle of its normal band."""
    pf = cfg.simulation.paint_filters
    if pf is None:
        return []
    lines = {ln.code for a in cfg.plant.areas if a.code == area for ln in a.lines}
    booths = [
        code
        for code, eq in cfg.equipment.items()
        if eq.type == pf.equipment_type and cfg.line_of_equipment(code).code in lines
    ]
    if not booths:
        return []
    signals = {s.code: s for s in cfg.equipment_types[pf.equipment_type].signals}
    out: list[dict[str, Any]] = []
    if pf.signal in signals:
        s = signals[pf.signal]
        out.append(
            {
                "name": s.code,
                "signal": s.code,
                "name_ru": s.name_ru,
                "unit": s.unit,
                "equipment": booths,
                "center": None,
            }
        )
    area_defects = cfg.simulation.defects.per_area.get(area)
    humidity = area_defects.humidity_signal if area_defects else None
    if humidity and humidity in signals:
        s = signals[humidity]
        center = band_center(s)
        out.append(
            {
                "name": f"{s.code}_deviation",
                "signal": s.code,
                "name_ru": f"Отклонение: {s.name_ru.lower()} от {num(center)} {s.unit}",
                "unit": s.unit,
                "equipment": booths,
                "center": center,
            }
        )
    return out


@router.get("/correlations", summary="Spearman ρ of the hourly defect rate vs process factors")
async def get_correlations(
    principal: Quality,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    area: Annotated[
        str, Query(description="area code (factors exist for the paint area)")
    ] = "PAINT",
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    _area_or_404(cfg, area)
    lo, hi = window(cfg, clock, start, end, default=timedelta(days=14), max_span=timedelta(days=60))
    lines = production_areas(cfg)[area]
    factors = correlation_factors(cfg, area)
    hourly = (
        (
            await session.execute(
                text(
                    "SELECT date_trunc('hour', ts) AS h, "
                    "count(*) FILTER (WHERE result = ANY(:first)) AS pq, "
                    "count(*) FILTER (WHERE result = ANY(:bad)) AS defects "
                    "FROM unit_event WHERE line = ANY(:lines) AND ts >= :lo AND ts < :hi GROUP BY 1"
                ),
                {
                    "first": sorted(FIRST_EXIT_RESULTS),
                    "bad": list(DEFECT_RESULTS),
                    "lines": lines,
                    "lo": lo,
                    "hi": hi,
                },
            )
        )
        .mappings()
        .all()
    )
    values: dict[tuple[str, datetime], list[float]] = defaultdict(list)
    for f in factors:
        for r in (
            await session.execute(
                text(
                    "SELECT bucket, avg FROM telemetry_1h WHERE equipment = ANY(:eq) "
                    "AND signal = :signal AND bucket >= :lo AND bucket < :hi"
                ),
                {"eq": f["equipment"], "signal": f["signal"], "lo": lo, "hi": hi},
            )
        ).all():
            v = float(r[1])
            values[(f["name"], r[0])].append(abs(v - f["center"]) if f["center"] is not None else v)
    stats = [
        HourStat(
            r["h"],
            int(r["pq"]),
            int(r["defects"]),
            {
                f["name"]: sum(values[(f["name"], r["h"])]) / len(values[(f["name"], r["h"])])
                for f in factors
                if values.get((f["name"], r["h"]))
            },
        )
        for r in hourly
    ]
    results = defect_correlations(stats, [f["name"] for f in factors])
    meta = {f["name"]: f for f in factors}
    area_name = cfg.areas[area].name_ru.lower()
    insights = []
    for res in results:
        f = meta[res.factor]
        step = max(1, len(res.points) // MAX_SCATTER)
        verb = "растёт" if res.direction > 0 else "снижается"
        p_text = "< 0,001" if res.p_value < 0.001 else f"= {num(round(res.p_value, 3))}"
        text_ru = (
            f"брак ({area_name}) {verb} вместе с фактором «{f['name_ru']}»: "
            f"ρ = {num(round(res.rho, 2))}, p {p_text}, {res.n} ч наблюдений"
            if res.insight
            else f"связь брака ({area_name}) с фактором «{f['name_ru']}» не обнаружена "
            f"(ρ = {num(round(res.rho, 2))}, p {p_text}, {res.n} ч)"
        )
        insights.append(
            {
                "factor": res.factor,
                "name_ru": f["name_ru"],
                "unit": f["unit"],
                "rho": round(res.rho, 4),
                "p_value": res.p_value,
                "n": res.n,
                "insight": res.insight,
                "direction": res.direction,
                "text_ru": text_ru,
                "points": [[round(x, 3), round(y, 4)] for x, y in res.points[::step]],
            }
        )
    return {
        "area": area,
        "from": lo.isoformat(),
        "to": hi.isoformat(),
        "hours": sum(1 for h in stats if h.pq > 0),
        "insights": insights,
    }
