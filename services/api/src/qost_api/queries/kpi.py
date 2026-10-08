"""KPI read model (SPEC §5.4, §12.2 ``GET /kpi``, FR-DB-02).

* Lines: the latest ``kpi_shift`` version per (line, date, shift, source); per §5.4 "Источники с
  приоритетом" an imported/manual report (``source='import'``) wins over the events, otherwise
  ``source='events'``. The running shift (not yet in ``kpi_shift``) is taken from the live view
  ``live:lines`` (``final: false``).
* Areas / plant: the same records aggregated with :func:`twin_core.kpi.aggregate_kpi` (sums of
  the time elements and counts, then the ratios); plus RTY = Π FPY of the lines involved.
* Equipment: availability, failures, MTBF/MTTR from ``equipment_state`` and ``downtime`` over the
  working shifts (same rules as the engine's live equipment metrics).

Everything reads aggregates or per-shift rows, never raw events (FR-DB-02, p95 ≤ 300 ms for a
month).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from twin_core.calendar import ShiftInstance
from twin_core.config import TwinConfig
from twin_core.kpi import ShiftKpi, aggregate_kpi, mtbf_h, mttr_min, rty

Level = Literal["line", "area", "plant", "equipment"]
Granularity = Literal["shift", "day", "month"]


@dataclass(frozen=True, slots=True)
class LineShift:
    """KPIs of one line for one shift with their provenance."""

    line: str
    day: date
    shift: str
    source: str
    final: bool
    version: int | None
    kpi: ShiftKpi


def _num(value: Any) -> float:
    return float(value or 0.0)


def kpi_from_values(v: Mapping[str, Any]) -> ShiftKpi:
    """A :class:`ShiftKpi` from a ``kpi_shift`` row or a live line view (same field names).

    ``Σ PRI(PQ)`` is not stored; it is ``E × APT`` (seconds).
    """
    apt = _num(v.get("apt"))
    e = v.get("effectiveness")
    pri_good = _num(v.get("pri_good_s"))
    pri_produced = float(e) * apt * 60.0 if e is not None else pri_good
    return ShiftKpi(
        pot_min=_num(v.get("pot")),
        pdot_min=_num(v.get("pdot")),
        pbt_min=_num(v.get("pbt")),
        apt_min=apt,
        adot_min=_num(v.get("adot")),
        adet_min=_num(v.get("adet")),
        aust_min=_num(v.get("aust")),
        microstop_min=_num(v.get("microstop_min")),
        pq=int(v.get("pq") or 0),
        gq=int(v.get("gq") or 0),
        pri_produced_s=pri_produced,
        pri_good_s=pri_good,
        availability=v.get("availability"),
        effectiveness=e,
        quality_ratio=v.get("quality_ratio"),
        oee=v.get("oee"),
        own_availability=None,
        defect_rate=v.get("defect_rate"),
        failures=v.get("failures"),
        repair_min=v.get("repair_min"),
        mtbf_h=v.get("mtbf_h"),
        mttr_min=v.get("mttr_min"),
    )


_LINE_ROWS = text(
    """
    SELECT DISTINCT ON (line, shift_date, shift_code, source) *
    FROM kpi_shift
    WHERE shift_date BETWEEN :d0 AND :d1 AND line = ANY(:lines)
    ORDER BY line, shift_date, shift_code, source, version DESC
    """
)


async def line_shifts(
    session: AsyncSession, lines: Sequence[str], d0: date, d1: date
) -> list[LineShift]:
    """Latest version per (line, date, shift), import winning over events (§5.4)."""
    rows = (
        await session.execute(_LINE_ROWS, {"d0": d0, "d1": d1, "lines": list(lines)})
    ).mappings()
    chosen: dict[tuple[str, date, str], LineShift] = {}
    for r in rows:
        key = (r["line"], r["shift_date"], r["shift_code"])
        rec = LineShift(
            line=r["line"],
            day=r["shift_date"],
            shift=r["shift_code"],
            source=r["source"],
            final=bool(r["final"]),
            version=int(r["version"]),
            kpi=kpi_from_values(dict(r)),
        )
        prev = chosen.get(key)
        if prev is None or (rec.source == "import" and prev.source != "import"):
            chosen[key] = rec
    return sorted(chosen.values(), key=lambda x: (x.day, x.shift, x.line))


def live_line_shifts(
    views: Mapping[str, Any],
    lines: Iterable[str],
    d0: date,
    d1: date,
    known: set[tuple[str, date, str]],
) -> list[LineShift]:
    """The running shift from ``live:lines`` when ``kpi_shift`` has no row for it yet."""
    out: list[LineShift] = []
    for code in lines:
        view = views.get(code)
        if not isinstance(view, dict):
            continue
        shift = view.get("shift")
        if not isinstance(shift, dict) or view.get("pot") is None:
            continue
        day = date.fromisoformat(str(shift["date"]))
        key = (code, day, str(shift["code"]))
        if not d0 <= day <= d1 or key in known:
            continue
        out.append(
            LineShift(code, day, str(shift["code"]), "events", False, None, kpi_from_values(view))
        )
    return out


def period_key(day: date, shift: str, granularity: Granularity) -> tuple[str, dict[str, Any]]:
    if granularity == "shift":
        return f"{day.isoformat()}/{shift}", {"date": day.isoformat(), "shift": shift}
    if granularity == "day":
        return day.isoformat(), {"date": day.isoformat()}
    month = f"{day.year:04d}-{day.month:02d}"
    return month, {"month": month}


def kpi_json(k: ShiftKpi) -> dict[str, Any]:
    return {
        "pot": k.pot_min,
        "pdot": k.pdot_min,
        "pbt": k.pbt_min,
        "apt": k.apt_min,
        "adot": k.adot_min,
        "adet": k.adet_min,
        "aust": k.aust_min,
        "microstop_min": k.microstop_min,
        "pq": k.pq,
        "gq": k.gq,
        "availability": k.availability,
        "effectiveness": k.effectiveness,
        "quality_ratio": k.quality_ratio,
        "oee": k.oee,
        "fpy": k.fpy,
        "defect_rate": k.defect_rate,
        "failures": k.failures,
        "repair_min": k.repair_min,
        "mtbf_h": k.mtbf_h,
        "mttr_min": k.mttr_min,
    }


def group_lines(level: Level, cfg: TwinConfig, code: str | None) -> dict[str, list[str]]:
    """Entity code -> its lines for the line/area/plant levels."""
    if level == "line":
        codes = [code] if code else list(cfg.flow_lines)
        return {c: [c] for c in codes}
    if level == "area":
        areas = [code] if code else [a.code for a in cfg.plant.areas if a.lines]
        return {a: [line.code for line in cfg.areas[a].lines] for a in areas}
    site = cfg.plant.site.code
    return {code or site: list(cfg.flow_lines)}


def aggregate_rows(
    records: Sequence[LineShift],
    groups: Mapping[str, list[str]],
    level: Level,
    granularity: Granularity,
) -> list[dict[str, Any]]:
    """One output row per (entity, period)."""
    out: list[dict[str, Any]] = []
    for entity, lines in groups.items():
        members = set(lines)
        buckets: dict[str, tuple[dict[str, Any], list[LineShift]]] = {}
        for rec in records:
            if rec.line not in members:
                continue
            key, period = period_key(rec.day, rec.shift, granularity)
            buckets.setdefault(key, (period, []))[1].append(rec)
        for key in sorted(buckets):
            period, recs = buckets[key]
            agg = recs[0].kpi if len(recs) == 1 else aggregate_kpi([r.kpi for r in recs])
            if agg is None:
                continue
            sources = {r.source for r in recs}
            row: dict[str, Any] = {
                "level": level,
                "code": entity,
                **period,
                **kpi_json(agg),
                "source": sources.pop() if len(sources) == 1 else "mixed",
                "final": all(r.final for r in recs),
                "shifts": len({(r.day, r.shift) for r in recs}),
            }
            if level in ("area", "plant"):
                by_line: dict[str, list[ShiftKpi]] = defaultdict(list)
                for r in recs:
                    by_line[r.line].append(r.kpi)
                line_fpy = [(aggregate_kpi(ks) or ks[0]).quality_ratio for ks in by_line.values()]
                row["rty"] = rty(line_fpy)
            out.append(row)
    return out


# --------------------------------------------------------------------------- equipment


_EQ_STATES = text(
    """
    SELECT entity, start_ts, end_ts, state FROM equipment_state
    WHERE entity = ANY(:codes) AND state IN ('DOWN_UNPLANNED', 'DOWN_PLANNED')
      AND start_ts < :hi AND (end_ts IS NULL OR end_ts > :lo)
    """
)
_EQ_STOPS = text(
    """
    SELECT entity, start_ts, end_ts FROM downtime
    WHERE import_id IS NULL AND entity = ANY(:codes) AND NOT planned AND NOT microstop
      AND start_ts < :hi AND COALESCE(end_ts, :now) > :lo
    """
)


def _clip(a: datetime, b: datetime, lo: datetime, hi: datetime) -> float:
    return max(0.0, (min(b, hi) - max(a, lo)).total_seconds())


async def equipment_rows(
    session: AsyncSession,
    cfg: TwinConfig,
    codes: Sequence[str],
    shifts: Sequence[ShiftInstance],
    now: datetime,
    granularity: Granularity,
) -> list[dict[str, Any]]:
    """Availability, failures, MTBF and MTTR of equipment over working shifts (events)."""
    windows = [(s, s.start, min(s.end, now)) for s in shifts if s.start < now]
    if not windows or not codes:
        return []
    lo = min(w[1] for w in windows)
    hi = max(w[2] for w in windows)
    params = {"codes": list(codes), "lo": lo, "hi": hi, "now": now}
    states = (await session.execute(_EQ_STATES, params)).mappings().all()
    stops = (await session.execute(_EQ_STOPS, params)).mappings().all()
    by_state: dict[str, list[Any]] = defaultdict(list)
    for r in states:
        by_state[r["entity"]].append(r)
    by_stop: dict[str, list[Any]] = defaultdict(list)
    for r in stops:
        by_stop[r["entity"]].append(r)
    out: list[dict[str, Any]] = []
    for code in codes:
        groups: dict[str, tuple[dict[str, Any], list[tuple[datetime, datetime]], list[bool]]] = {}
        for s, w0, w1 in windows:
            key, period = period_key(s.shift_date, s.code, granularity)
            group = groups.setdefault(key, (period, [], []))
            group[1].append((w0, w1))
            group[2].append(s.end <= now)
        for key in sorted(groups):
            period, spans, closed = groups[key]
            total = sum((b - a).total_seconds() for a, b in spans)
            down_u = down_p = 0.0
            for r in by_state.get(code, []):
                end = r["end_ts"] or now
                sec = sum(_clip(r["start_ts"], end, a, b) for a, b in spans)
                if r["state"] == "DOWN_UNPLANNED":
                    down_u += sec
                else:
                    down_p += sec
            failures = 0
            repair = 0.0
            for r in by_stop.get(code, []):
                end = r["end_ts"] or now
                sec = sum(_clip(r["start_ts"], end, a, b) for a, b in spans)
                if sec > 0:
                    failures += 1
                    repair += sec / 60.0
            base = total - down_p
            run_min = max(total - down_u - down_p, 0.0) / 60.0
            out.append(
                {
                    "level": "equipment",
                    "code": code,
                    **period,
                    "pot": total / 60.0,
                    "pdot": down_p / 60.0,
                    "pbt": base / 60.0,
                    "apt": max(base - down_u, 0.0) / 60.0,
                    "adot": down_u / 60.0,
                    "adet": None,
                    "aust": None,
                    "microstop_min": None,
                    "pq": None,
                    "gq": None,
                    "availability": None if base <= 0 else (base - down_u) / base,
                    "effectiveness": None,
                    "quality_ratio": None,
                    "oee": None,
                    "fpy": None,
                    "defect_rate": None,
                    "failures": failures,
                    "repair_min": repair,
                    "mtbf_h": mtbf_h(run_min, failures),
                    "mttr_min": mttr_min(repair, failures),
                    "source": "events",
                    "final": all(closed),
                    "shifts": len(spans),
                }
            )
    return out
