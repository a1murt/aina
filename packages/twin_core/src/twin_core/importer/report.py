"""Import report: KPIs, reconciliation and alerts for an imported period (FR-IMP-04/05).

:func:`build_import_report` turns a :class:`ParsedImport` into an :class:`ImportReport`;
:meth:`ImportReport.to_json` is the structure of ``data/case/expected/import_expected.json``
(golden, produced by the reference script ``compute_expected.py``) plus a few extra keys
(``constraint_checks``, ``warnings``, extra ``meta`` fields) that the golden test ignores.

Aggregate path (SPEC §5.4): the line table is one shift (the first calendar shift, D1); POT =
shift length, PDOT = ADET = 0, APT = reported working time; PQ = «Факт», GQ = «Факт» − «Брак»
(defects are reworked, D4). The downtime log is per day with unknown shift (DQ-03).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import Any, Final

from twin_core.bottleneck import AggregateBottleneck, aggregate_bottleneck
from twin_core.config import TwinConfig
from twin_core.config.rules import DataQualityThresholds
from twin_core.dq import (
    DqIssue,
    check_downtime_reconciliation,
    check_effectiveness,
    check_flow_balance,
    check_load_mismatch,
    check_plan_vs_target,
    check_records_without_shift,
)
from twin_core.importer.constraints import (
    ConstraintCheck,
    configured_constraints,
    parse_constraints,
    resolve_constraints,
)
from twin_core.importer.model import ImportFormatError, UploadedFile
from twin_core.importer.parse import LineRow, ParsedImport, PlanRow, QualityRow, parse_upload
from twin_core.importer.readers import read_upload
from twin_core.kpi import (
    SECONDS_PER_MINUTE,
    ShiftKpi,
    aggregate_shift_kpi,
    capacity_loss_min,
    defect_rate,
    minutes_to_units,
    plan_gap,
    ratio,
    required_rate,
    round_fraction,
)
from twin_core.rules import Alert, AlertEvaluator, deduplicate

FLOAT_TOLERANCE: Final = 1e-4
_MONTHS: Final = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def _r4(value: float | None) -> float | None:
    return round_fraction(value)


def _whole(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class ShiftReportRecord:
    """One imported shift report (``shift_report`` + ``kpi_shift`` with ``source=import``)."""

    day: date
    shift: str
    line: str
    area: str
    plan_qty: int | None
    reported_load_pct: float | None
    reported_defect_pct: float | None
    kpi: ShiftKpi

    def to_report(self) -> dict[str, Any]:
        k = self.kpi
        return {
            "date": self.day.isoformat(),
            "shift": self.shift,
            "line": self.line,
            "area": self.area,
            "plan_qty": self.plan_qty,
            "produced_qty": k.pq,
            "defect_qty": k.defects,
            "good_qty": k.gq,
            "worked_min": _r4(k.apt_min),
            "reported_load_pct": self.reported_load_pct,
            "reported_defect_pct": self.reported_defect_pct,
            "pot_min": _whole(k.pot_min),
            "pdot_min": _whole(k.pdot_min),
            "pbt_min": _whole(k.pbt_min),
            "apt_min": _r4(k.apt_min),
            "lost_min": _r4(k.lost_min),
            "availability": _r4(k.availability),
            "effectiveness": _r4(k.effectiveness),
            "quality_ratio": _r4(k.quality_ratio),
            "oee": _r4(k.oee),
            "defect_rate": _r4(k.defect_rate),
            "fpy": _r4(k.fpy),
        }


@dataclass(frozen=True, slots=True)
class DowntimeRecord:
    """One imported downtime log entry (``downtime`` with ``reason_source=import``)."""

    day: date
    area: str
    line: str
    equipment: str
    reason_code: str
    reason_text_src: str
    planned: bool
    duration_min: float
    criticality: str
    degraded_capacity: float
    effective_capacity_loss_min: float
    capacity_loss_units: float | None
    shift: str | None = None

    def to_report(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "area": self.area,
            "equipment": self.equipment,
            "reason_code": self.reason_code,
            "reason_text_src": self.reason_text_src,
            "planned": self.planned,
            "duration_min": self.duration_min,
            "shift": self.shift,
            "criticality": self.criticality,
            "degraded_capacity": self.degraded_capacity,
            "effective_capacity_loss_min": _r4(self.effective_capacity_loss_min),
            "capacity_loss_units": _r4(self.capacity_loss_units),
        }


@dataclass(frozen=True, slots=True)
class PlanSummary:
    """Month plan vs target and the rates it requires (SPEC §5.8)."""

    month: str
    rows: tuple[PlanRow, ...]
    line: str | None
    """Line the model plan belongs to (from ``plant.yaml: plan``)."""
    line_model_total: int
    plant_target: int
    working_days: int
    shifts: int
    mean_sustainable_rate: float | None
    """Mean over days of the smallest daily output along the flow (cars per shift)."""

    @property
    def gap(self) -> int:
        return int(plan_gap(self.line_model_total, self.plant_target))

    def to_report(self) -> dict[str, Any]:
        year, month = self.month.split("-")
        suffix = f"{_MONTHS[int(month) - 1]}_{year}"
        mean = self.mean_sustainable_rate
        return {
            "rows": [{"model": r.model, "model_src": r.model_src, "qty": r.qty} for r in self.rows],
            "line_model_total": self.line_model_total,
            "plant_target": self.plant_target,
            "gap": self.gap,
            f"working_days_{suffix}": self.working_days,
            f"shifts_{suffix}": self.shifts,
            "required_rate_per_shift_line_plan": _r4(
                required_rate(self.line_model_total, 0, self.shifts)
            ),
            "required_rate_per_shift_target": _r4(required_rate(self.plant_target, 0, self.shifts)),
            "mean_sustainable_rate_per_shift": _r4(mean),
            "naive_month_projection": None if mean is None else round(mean * self.shifts),
        }


@dataclass(frozen=True, slots=True)
class FlowSummary:
    """Output along the flow over the imported period (SPEC §5.6), by area in flow order."""

    produced_total: Mapping[str, int]
    good_total: Mapping[str, int]
    mean_produced_per_shift: Mapping[str, float]

    def to_report(self) -> dict[str, Any]:
        return {
            "produced_total": dict(self.produced_total),
            "good_total": dict(self.good_total),
            "mean_produced_per_shift": {
                area: _r4(value) for area, value in self.mean_produced_per_shift.items()
            },
        }


@dataclass(frozen=True, slots=True)
class ImportReport:
    source: str
    meta: Mapping[str, Any]
    constraints: Mapping[str, int | float]
    constraint_checks: tuple[ConstraintCheck, ...]
    shift_reports: tuple[ShiftReportRecord, ...]
    downtime: tuple[DowntimeRecord, ...]
    plan: PlanSummary
    flow: FlowSummary
    bottleneck: AggregateBottleneck
    dq_issues: tuple[DqIssue, ...]
    alerts: tuple[Alert, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def period(self) -> tuple[date, date]:
        days = [r.day for r in self.shift_reports]
        return min(days), max(days)

    def to_json(self) -> dict[str, Any]:
        """JSON-ready report; the golden keys first, in the golden order."""
        return {
            "meta": dict(self.meta),
            "constraints_parsed": dict(self.constraints),
            "shift_reports": [r.to_report() for r in self.shift_reports],
            "downtime": [d.to_report() for d in self.downtime],
            "plan": self.plan.to_report(),
            "flow": self.flow.to_report(),
            "bottleneck_aggregate": self.bottleneck.to_report(),
            "data_quality_issues": [i.to_report() for i in self.dq_issues],
            "alerts": [a.to_report() for a in self.alerts],
            "constraint_checks": [c.to_report() for c in self.constraint_checks],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- builder


def _plan_line(cfg: TwinConfig, month: str, rows: Sequence[PlanRow]) -> str | None:
    by_product = {
        entry.product: entry.line
        for entry in cfg.plant.plan
        if entry.level == "line_model" and entry.month == month
    }
    lines = {by_product.get(r.model) for r in rows} - {None}
    if len(lines) == 1:
        return lines.pop()
    fallback = [e.line for e in cfg.plant.plan if e.level == "line_model" and e.line]
    return fallback[0] if fallback else None


def _shift_reports(
    parsed: ParsedImport, cfg: TwinConfig, warnings: list[str]
) -> list[ShiftReportRecord]:
    shift_code = cfg.calendar.shift_codes[0]
    shift = next(s for s in cfg.plant.calendar.shifts if s.code == shift_code)
    quality: dict[tuple[date, str], QualityRow] = {}
    for item in parsed.quality:
        key = (item.day, item.area)
        if key in quality:
            warnings.append(
                f"quality: duplicate row for {item.day.isoformat()} {item.area}, last kept"
            )
        quality[key] = item
    latest: dict[tuple[date, str], LineRow] = {}
    for row in parsed.lines:
        if (row.day, row.line) in latest:
            warnings.append(f"lines: duplicate row for {row.day.isoformat()} {row.line}, last kept")
        latest[(row.day, row.line)] = row
    order = {code: i for i, code in enumerate(cfg.flow_lines)}
    records: list[ShiftReportRecord] = []
    for row in sorted(latest.values(), key=lambda r: (r.day, order[r.line])):
        area = cfg.area_of_line(row.line).code
        q = quality.get((row.day, area))
        defects = 0
        if q is None:
            warnings.append(f"quality: no row for {row.day.isoformat()} {area}, defects taken as 0")
        else:
            defects = q.defects
            if q.produced is not None and q.produced != row.produced:
                warnings.append(
                    f"quality: {row.day.isoformat()} {area} produced {q.produced}, "
                    f"line table says {row.produced}; the line table is used"
                )
        if defects > row.produced:
            warnings.append(
                f"quality: {row.day.isoformat()} {area} defects {defects} exceed produced "
                f"{row.produced}; capped"
            )
            defects = row.produced
        if not cfg.calendar.is_working_day(row.day):
            warnings.append(f"lines: {row.day.isoformat()} is not a working day in the calendar")
        kpi = aggregate_shift_kpi(
            pot_min=shift.duration_min,
            worked_min=row.worked_min,
            produced=row.produced,
            good=row.produced - defects,
            ict_seconds=cfg.lines[row.line].ict_seconds,
        )
        records.append(
            ShiftReportRecord(
                day=row.day,
                shift=shift_code,
                line=row.line,
                area=area,
                plan_qty=row.plan_qty,
                reported_load_pct=row.reported_load_pct,
                reported_defect_pct=q.defect_pct if q else None,
                kpi=kpi,
            )
        )
    return records


def _downtime(parsed: ParsedImport, cfg: TwinConfig) -> list[DowntimeRecord]:
    records = []
    for d in parsed.downtime:
        equipment = cfg.equipment[d.equipment]
        reason = cfg.reasons[d.reason_code]
        line = cfg.line_of_equipment(d.equipment)
        loss = capacity_loss_min(
            d.duration_min, equipment.degraded_capacity, planned=reason.planned
        )
        records.append(
            DowntimeRecord(
                day=d.day,
                area=d.area,
                line=line.code,
                equipment=d.equipment,
                reason_code=d.reason_code,
                reason_text_src=d.reason_src,
                planned=reason.planned,
                duration_min=d.duration_min,
                criticality=equipment.criticality,
                degraded_capacity=equipment.degraded_capacity,
                effective_capacity_loss_min=loss,
                capacity_loss_units=minutes_to_units(loss, line.ict_seconds),
            )
        )
    return records


def _area_order(cfg: TwinConfig, reports: Sequence[ShiftReportRecord]) -> list[str]:
    present = {r.area for r in reports}
    return [area for area in cfg.areas if area in present]


def _data_quality(
    reports: Sequence[ShiftReportRecord],
    downtime: Sequence[DowntimeRecord],
    flow: FlowSummary,
    plan: PlanSummary,
    thresholds: DataQualityThresholds,
) -> list[DqIssue]:
    issues: list[DqIssue | None] = []
    for r in reports:
        issues.append(
            check_load_mismatch(
                line=r.line,
                period_date=r.day,
                reported_load_pct=r.reported_load_pct,
                availability=r.kpi.availability,
                thresholds=thresholds,
            )
        )
    logged: dict[tuple[date, str], float] = defaultdict(float)
    for d in downtime:
        logged[(d.day, d.area)] += d.duration_min
    lost: dict[tuple[date, str], float] = {}
    for r in reports:
        key = (r.day, r.area)
        lost[key] = lost.get(key, 0.0) + (_r4(r.kpi.lost_min) or 0.0)
    for (day, area), lost_min in lost.items():
        issues.append(
            check_downtime_reconciliation(
                area=area,
                period_date=day,
                logged_min=logged.get((day, area), 0.0),
                lost_min=lost_min,
                thresholds=thresholds,
            )
        )
    issues.append(check_records_without_shift(sum(1 for d in downtime if d.shift is None)))
    areas = list(flow.produced_total)
    for upstream, downstream in pairwise(areas):
        issues.append(
            check_flow_balance(
                upstream=upstream,
                downstream=downstream,
                upstream_produced=flow.produced_total[upstream],
                downstream_produced=flow.produced_total[downstream],
                thresholds=thresholds,
            )
        )
    issues.append(
        check_plan_vs_target(line_model_plan=plan.line_model_total, plant_target=plan.plant_target)
    )
    for r in reports:
        issues.append(
            check_effectiveness(line=r.line, period_date=r.day, effectiveness=r.kpi.effectiveness)
        )
    return [i for i in issues if i is not None]


def _alerts(
    reports: Sequence[ShiftReportRecord],
    downtime: Sequence[DowntimeRecord],
    areas: Sequence[str],
    evaluator: AlertEvaluator,
) -> list[Alert]:
    alerts: list[Alert | None] = []
    for r in reports:
        alerts.append(
            evaluator.defect_rate(
                area=r.area, period_date=r.day, shift=r.shift, defect_rate=r.kpi.defect_rate
            )
        )
        alerts.append(evaluator.oee(line=r.line, period_date=r.day, shift=r.shift, oee=r.kpi.oee))
    unplanned_a: dict[tuple[date, str], float] = defaultdict(float)
    for d in downtime:
        if d.criticality == "A" and not d.planned:
            unplanned_a[(d.day, d.equipment)] += d.duration_min
    for (day, equipment), minutes in sorted(unplanned_a.items()):
        alerts.append(
            evaluator.critical_downtime(equipment=equipment, period_date=day, unplanned_min=minutes)
        )
    counts: dict[date, dict[str, list[int]]] = defaultdict(dict)
    shifts: dict[date, str] = {}
    for r in reports:
        pq_gq = counts[r.day].setdefault(r.area, [0, 0])
        pq_gq[0] += r.kpi.pq
        pq_gq[1] += r.kpi.gq
        shifts[r.day] = r.shift
    rates = {
        day: {a: defect_rate(*by_area[a]) for a in areas if a in by_area}
        for day, by_area in counts.items()
    }
    days = sorted(rates)
    for previous, current in pairwise(days):
        alerts.append(
            evaluator.systemic_defects(
                period_date=current,
                previous=rates[previous],
                current=rates[current],
                shift=shifts[current],
            )
        )
    return deduplicate(a for a in alerts if a is not None)


def build_import_report(
    parsed: ParsedImport,
    cfg: TwinConfig,
    *,
    source: str,
    evaluator: AlertEvaluator | None = None,
    dq_thresholds: DataQualityThresholds | None = None,
) -> ImportReport:
    """Compute the import report of a parsed upload (pure: no I/O, no clock).

    Thresholds come from ``rules.yaml``; pass ``evaluator`` / ``dq_thresholds`` to apply
    overrides. Raises :class:`ImportFormatError` when no usable line row is left.
    """
    warnings = list(parsed.warnings)
    reports = _shift_reports(parsed, cfg, warnings)
    if not reports:
        raise ImportFormatError(
            "no usable rows in the lines table",
            problems=[
                f"{i.details.get('column')}: {i.details.get('value')!r}" for i in parsed.issues
            ],
        )
    downtime = _downtime(parsed, cfg)
    first_day = min(r.day for r in reports)
    month = f"{first_day:%Y-%m}"

    parsed_constraints = parse_constraints(parsed.text)
    constraints, checks = resolve_constraints(
        parsed_constraints, configured_constraints(cfg, month)
    )

    areas = _area_order(cfg, reports)
    output: dict[date, dict[str, int]] = defaultdict(dict)
    good: dict[str, int] = defaultdict(int)
    for r in reports:
        output[r.day][r.area] = output[r.day].get(r.area, 0) + r.kpi.pq
        good[r.area] += r.kpi.gq
    bottleneck = aggregate_bottleneck(output, areas)
    flow = FlowSummary(
        produced_total={a: sum(day.get(a, 0) for day in output.values()) for a in areas},
        good_total={a: good[a] for a in areas},
        mean_produced_per_shift=dict(bottleneck.mean_output),
    )

    daily_min = [min(day[a] for a in areas if a in day) for day in output.values()]
    year, month_no = first_day.year, first_day.month
    plan = PlanSummary(
        month=month,
        rows=tuple(parsed.plan),
        line=_plan_line(cfg, month, parsed.plan),
        line_model_total=sum(r.qty for r in parsed.plan),
        plant_target=int(constraints["plant_target_per_month"]),
        working_days=len(cfg.calendar.working_days_in_month(year, month_no)),
        shifts=len(cfg.calendar.working_shifts_in_month(year, month_no)),
        mean_sustainable_rate=ratio(sum(daily_min), len(daily_min)),
    )

    thresholds = dq_thresholds or cfg.rules.data_quality
    issues = _data_quality(reports, downtime, flow, plan, thresholds) + list(parsed.issues)
    alerts = _alerts(reports, downtime, areas, evaluator or AlertEvaluator(cfg.rules))

    icts = {cfg.lines[r.line].ict_seconds for r in reports}
    shift_code = reports[0].shift
    bdr = [r.kpi.apt_min * SECONDS_PER_MINUTE / r.kpi.pq for r in reports if r.kpi.pq > 0]
    meta: dict[str, Any] = {
        "source": source,
        "ict_seconds": _whole(icts.pop())
        if len(icts) == 1
        else {r.line: _whole(cfg.lines[r.line].ict_seconds) for r in reports},
        "ict_seconds_bdr": _r4(min(bdr)) if bdr else None,
        "shift_minutes": _whole(reports[0].kpi.pot_min),
        "assumptions": [
            f"line table = one shift ({shift_code})",
            "downtime log is per day, shift unknown",
            "defects are rework (produced counts include defective units)",
            "pdot=0 for imported shifts because planned downtime cannot be attributed to a shift",
        ],
        "float_tolerance": FLOAT_TOLERANCE,
        "kind": parsed.kind,
        "files": list(parsed.files),
        "period": {"from": first_day.isoformat(), "to": max(r.day for r in reports).isoformat()},
        "shift": shift_code,
    }
    return ImportReport(
        source=source,
        meta=meta,
        constraints=constraints,
        constraint_checks=tuple(checks),
        shift_reports=tuple(reports),
        downtime=tuple(downtime),
        plan=plan,
        flow=flow,
        bottleneck=bottleneck,
        dq_issues=tuple(issues),
        alerts=tuple(alerts),
        warnings=tuple(warnings),
    )


def run_import(
    files: Sequence[UploadedFile], cfg: TwinConfig, *, source: str | None = None
) -> ImportReport:
    """Read, recognize, parse and evaluate an upload in one call."""
    upload = read_upload(files)
    parsed = parse_upload(upload, cfg)
    return build_import_report(parsed, cfg, source=source or ", ".join(f.name for f in files))
