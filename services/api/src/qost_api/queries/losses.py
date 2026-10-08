"""Loss tree for a period (FR-KPI-05, ``GET /kpi/losses``): minutes, cars and ₸.

Per line, the time elements of the chosen shift KPIs (:mod:`qost_api.queries.kpi`, import over
events) are summed into one :class:`~twin_core.kpi.TimeModel` and passed to
:func:`twin_core.kpi.line_loss_tree`, so the categories partition POT exactly:

* ADET is split into starved/blocked by the line's ``STARVED``/``BLOCKED`` intervals in the
  event-sourced shifts (imported shifts have ADET = 0);
* ADOT is broken down by reason and causing unit: event-sourced shifts use the line's unplanned
  stops (≥ microstop threshold) clipped to the shift, the causing unit is the class-A stop that
  started at the same instant; imported shifts use the day's imported journal (capacity loss
  §5.7 = minutes × (1 − degraded_capacity)); the attribution is scaled down if it exceeds ADOT.

Cars = minutes × 60 / ICT of the line; ₸ = cars × ``avg_price_kzt`` × ``margin_rate``
(business.yaml, the margin is an assumption, §10.5: no revenue). Planned downtime is listed for
completeness but is not a loss (§5.7: excluded from PBT) and is left out of the totals.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.queries.kpi import LineShift
from twin_core.config import TwinConfig
from twin_core.kpi import DowntimeShare, LossCategory, TimeModel, line_loss_tree

_FLOW = text(
    """
    SELECT entity, state, start_ts, end_ts FROM equipment_state
    WHERE entity = ANY(:lines) AND state IN ('STARVED', 'BLOCKED')
      AND start_ts < :hi AND (end_ts IS NULL OR end_ts > :lo)
    """
)
_LINE_STOPS = text(
    """
    SELECT l.entity AS line, l.start_ts, l.end_ts, l.reason_code, e.entity AS equipment
    FROM downtime l
    LEFT JOIN LATERAL (
        SELECT d.entity FROM downtime d
        WHERE d.import_id IS NULL AND d.line = l.line AND d.entity <> l.line
          AND d.start_ts = l.start_ts
        ORDER BY d.entity LIMIT 1
    ) e ON TRUE
    WHERE l.import_id IS NULL AND l.entity = ANY(:lines) AND NOT l.planned AND NOT l.microstop
      AND l.start_ts < :hi AND COALESCE(l.end_ts, :now) > :lo
    """
)
_IMPORT_STOPS = text(
    """
    SELECT entity, line, shift_date, duration_s, reason_code FROM downtime
    WHERE import_id IS NOT NULL AND NOT planned AND line = ANY(:lines)
      AND shift_date = ANY(:days)
    """
)


def _clip(a: datetime, b: datetime, lo: datetime, hi: datetime) -> float:
    return max(0.0, (min(b, hi) - max(a, lo)).total_seconds())


async def loss_inputs(
    session: AsyncSession,
    cfg: TwinConfig,
    records: Sequence[LineShift],
    now: datetime,
) -> tuple[dict[str, tuple[float, float]], dict[str, list[DowntimeShare]]]:
    """Starved/blocked minutes and ADOT attribution per line (see the module docstring)."""
    cal = cfg.calendar
    windows: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    import_days: dict[str, set[date]] = defaultdict(set)
    for rec in records:
        if rec.source == "import":
            import_days[rec.line].add(rec.day)
            continue
        inst = cal.shift(rec.day, rec.shift)
        if inst.start < now:
            windows[rec.line].append((inst.start, min(inst.end, now)))
    flow: dict[str, tuple[float, float]] = {}
    shares: dict[str, list[DowntimeShare]] = defaultdict(list)
    lines = sorted(windows)
    if lines:
        lo = min(a for w in windows.values() for a, _ in w)
        hi = max(b for w in windows.values() for _, b in w)
        params = {"lines": lines, "lo": lo, "hi": hi, "now": now}
        starved: dict[str, float] = defaultdict(float)
        blocked: dict[str, float] = defaultdict(float)
        for r in (await session.execute(_FLOW, params)).mappings():
            sec = sum(
                _clip(r["start_ts"], r["end_ts"] or now, a, b) for a, b in windows[r["entity"]]
            )
            (starved if r["state"] == "STARVED" else blocked)[r["entity"]] += sec / 60.0
        flow = {line: (starved[line], blocked[line]) for line in lines}
        grouped: dict[tuple[str, str | None, str | None], float] = defaultdict(float)
        for r in (await session.execute(_LINE_STOPS, params)).mappings():
            sec = sum(_clip(r["start_ts"], r["end_ts"] or now, a, b) for a, b in windows[r["line"]])
            if sec > 0:
                grouped[(r["line"], r["reason_code"], r["equipment"])] += sec / 60.0
        for (line, reason, equipment), minutes in grouped.items():
            shares[line].append(DowntimeShare(minutes, reason, equipment))
    days = sorted({d for ds in import_days.values() for d in ds})
    if days:
        rows = await session.execute(_IMPORT_STOPS, {"lines": sorted(import_days), "days": days})
        grouped_i: dict[tuple[str, str | None, str | None], float] = defaultdict(float)
        for r in rows.mappings():
            if r["shift_date"] not in import_days[r["line"]]:
                continue
            eq = cfg.equipment.get(r["entity"])
            capacity = eq.degraded_capacity if eq is not None else 0.0
            minutes = float(r["duration_s"] or 0.0) / 60.0 * (1.0 - capacity)
            if minutes > 0:
                grouped_i[(r["line"], r["reason_code"], r["entity"])] += minutes
        for (line, reason, equipment), minutes in grouped_i.items():
            shares[line].append(DowntimeShare(minutes, reason, equipment))
    return flow, dict(shares)


def units_of(item: dict[str, Any]) -> float:
    value = item.get("units")
    return float(value) if isinstance(value, int | float) else 0.0


def loss_tree_view(
    cfg: TwinConfig,
    records: Sequence[LineShift],
    flow: Mapping[str, tuple[float, float]],
    shares: Mapping[str, list[DowntimeShare]],
    lines: Sequence[str],
) -> dict[str, Any]:
    """Categories and items (minutes, cars, ₸) over ``lines``."""
    params = cfg.business.params
    kzt_per_car = params.avg_price_kzt.value * params.margin_rate.value
    by_line: dict[str, list[LineShift]] = defaultdict(list)
    for rec in records:
        by_line[rec.line].append(rec)
    items: dict[tuple[str, str | None, str | None, str | None], list[float]] = defaultdict(
        lambda: [0.0, 0.0]
    )
    shifts = {"events": 0, "import": 0}
    for line in lines:
        recs = by_line.get(line, [])
        if not recs:
            continue
        for r in recs:
            shifts["import" if r.source == "import" else "events"] += 1
        ks = [r.kpi for r in recs]
        pot = sum(k.pot_min for k in ks)
        pdot = min(sum(k.pdot_min for k in ks), pot)
        adot = sum(k.adot_min for k in ks)
        adet = sum(k.adet_min for k in ks)
        aust = sum(k.aust_min for k in ks)
        s_min, b_min = flow.get(line, (0.0, 0.0))
        measured = s_min + b_min
        starved = adet * s_min / measured if measured > 0 else adet
        blocked = adet - starved
        apt = pot - pdot - adot - adet - aust
        micro = min(sum(k.microstop_min for k in ks), max(apt, 0.0))
        time_model = TimeModel(
            pot=pot,
            pdot=pdot,
            adot=adot,
            starved=max(starved, 0.0),
            blocked=max(blocked, 0.0),
            aust=aust,
            microstop=micro,
        )
        line_shares = shares.get(line, [])
        attributed = sum(s.minutes for s in line_shares)
        if attributed > adot > 0:
            line_shares = [
                DowntimeShare(s.minutes * adot / attributed, s.reason_code, s.equipment)
                for s in line_shares
            ]
        elif adot <= 0:
            line_shares = []
        tree = line_loss_tree(
            line=line,
            ict_seconds=cfg.lines[line].ict_seconds,
            time=time_model,
            pri_produced_s=sum(k.pri_produced_s for k in ks),
            pri_good_s=sum(k.pri_good_s for k in ks),
            unplanned=line_shares,
        )
        for item in tree.items:
            if item.minutes == 0:
                continue
            acc = items[(item.category.value, line, item.reason_code, item.equipment)]
            acc[0] += item.minutes
            acc[1] += item.units or 0.0
    categories: dict[str, list[float]] = {c.value: [0.0, 0.0] for c in LossCategory}
    out_items: list[dict[str, Any]] = []
    for (category, code, reason, equipment), (minutes, units) in items.items():
        categories[category][0] += minutes
        categories[category][1] += units
        out_items.append(
            {
                "category": category,
                "line": code,
                "area": cfg.area_of_line(code).code if code else None,
                "reason_code": reason,
                "equipment": equipment,
                "minutes": round(minutes, 2),
                "units": round(units, 2),
                "kzt": round(units * kzt_per_car),
                "loss": category != LossCategory.PLANNED_DOWNTIME.value,
            }
        )
    out_items.sort(key=lambda x: (not x["loss"], -units_of(x)))
    loss_minutes = sum(
        v[0] for c, v in categories.items() if c != LossCategory.PLANNED_DOWNTIME.value
    )
    loss_units = sum(
        v[1] for c, v in categories.items() if c != LossCategory.PLANNED_DOWNTIME.value
    )
    return {
        "currency": cfg.business.currency,
        "kzt_per_car": kzt_per_car,
        "assumptions": {
            "avg_price_kzt": params.avg_price_kzt.assumption,
            "margin_rate": params.margin_rate.assumption,
        },
        "totals": {
            "minutes": round(loss_minutes, 2),
            "units": round(loss_units, 2),
            "kzt": round(loss_units * kzt_per_car),
        },
        "categories": [
            {
                "category": c,
                "minutes": round(v[0], 2),
                "units": round(v[1], 2),
                "kzt": round(v[1] * kzt_per_car),
                "loss": c != LossCategory.PLANNED_DOWNTIME.value,
            }
            for c, v in categories.items()
        ],
        "items": out_items,
        "shifts": shifts,
    }
