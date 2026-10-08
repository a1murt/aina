"""Pure assembly of a :class:`ShiftInput` from the facts of one shift (rows as the engine and the
import write them, SPEC §8) — no I/O; the API reads the rows (``qost_api.reports.data``).

Rules:

* KPIs per line: the latest version, ``events`` preferred over ``import``;
* plant output = good cars (GQ) of the last flow line, plan = its ``plan_rate_per_shift``;
* losses (FR-KPI-05) per line from the stored time model (ADET is one «flow delay» item: the
  shift row does not split starved/blocked); speed loss uses PRI(PQ) = E × APT;
* stops: equipment-level, not microstops, clipped to the shift, longest first (top
  ``MAX_STOPS``); microstops are counted on line level;
* deviations: OEE below target, area defect rate above the limit, output below plan, month
  forecast risk (``plan_risk_warn_p``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from twin_core.calendar import ShiftInstance
from twin_core.clock import ensure_utc, to_plant_tz
from twin_core.config import TwinConfig
from twin_core.forecast.result import ForecastResult
from twin_core.kpi import LossCategory, TimeModel, line_loss_tree
from twin_core.report.model import (
    AlertRow,
    AreaQuality,
    BottleneckRow,
    DefectRow,
    Deviation,
    ForecastSummary,
    Lang,
    LineRow,
    LossRow,
    ShiftInput,
    ShiftRef,
    StopRow,
    Thresholds,
    Totals,
)

MAX_STOPS = 8
MAX_DEFECTS = 8
MAX_ALERTS = 10
MAX_LOSSES_PER_LINE = 4
MIN_LOSS_MIN = 1.0

_LOSS_NAMES: dict[str, dict[str, str]] = {
    "ru": {
        LossCategory.PLANNED_DOWNTIME: "плановые простои",
        LossCategory.UNPLANNED_DOWNTIME: "внеплановые простои",
        LossCategory.STARVED: "задержки потока",
        LossCategory.BLOCKED: "блокировка",
        LossCategory.CHANGEOVER: "переналадка",
        LossCategory.MICROSTOPS: "микроостановки",
        LossCategory.SPEED: "потеря скорости",
        LossCategory.QUALITY: "брак",
    },
    "kk": {
        LossCategory.PLANNED_DOWNTIME: "жоспарлы тоқтаулар",
        LossCategory.UNPLANNED_DOWNTIME: "жоспардан тыс тоқтаулар",
        LossCategory.STARVED: "ағын кідірістері",
        LossCategory.BLOCKED: "бұғатталу",
        LossCategory.CHANGEOVER: "қайта баптау",
        LossCategory.MICROSTOPS: "микротоқтаулар",
        LossCategory.SPEED: "жылдамдық шығыны",
        LossCategory.QUALITY: "ақау",
    },
}


@dataclass(frozen=True, slots=True)
class KpiFact:
    """A ``kpi_shift`` row (minutes; fractions 0..1)."""

    line: str
    source: str
    version: int
    final: bool
    pot: float
    pdot: float
    pbt: float
    apt: float
    adot: float
    adet: float
    aust: float
    microstop_min: float
    pq: int
    gq: int
    pri_good_s: float
    availability: float | None
    effectiveness: float | None
    quality_ratio: float | None
    oee: float | None
    defect_rate: float | None
    failures: int | None = None
    repair_min: float | None = None


@dataclass(frozen=True, slots=True)
class StopFact:
    """A ``downtime`` row."""

    entity: str
    line: str
    start: datetime | None
    end: datetime | None
    duration_s: float | None
    planned: bool
    microstop: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class DefectFact:
    area: str
    defect_code: str
    qty: int


@dataclass(frozen=True, slots=True)
class AlertFact:
    rule_id: str
    severity: str
    entity_type: str
    entity: str
    ts: datetime
    title_ru: str
    message_ru: str
    status: str
    title_kk: str | None = None
    message_kk: str | None = None


@dataclass(frozen=True, slots=True)
class BottleneckFact:
    line: str
    sole_share: float
    shifting_share: float


@dataclass(frozen=True, slots=True)
class ShiftFacts:
    shift: ShiftInstance
    now: datetime
    """Plant time of the assembly (open stops end here)."""
    kpis: Sequence[KpiFact] = ()
    stops: Sequence[StopFact] = ()
    defects: Sequence[DefectFact] = ()
    alerts: Sequence[AlertFact] = ()
    bottleneck: Sequence[BottleneckFact] = ()
    forecast: ForecastResult | None = None
    notes: Sequence[str] = field(default_factory=tuple)


# --------------------------------------------------------------------------- helpers


def pct1(value: float | None) -> float | None:
    """Fraction -> percent with one decimal (``0.81234`` -> ``81.2``)."""
    return None if value is None else round(value * 100.0, 1)


def _hhmm(moment: datetime, cfg: TwinConfig) -> str:
    return to_plant_tz(moment, cfg.timezone).strftime("%H:%M")


def _ref(shift: ShiftInstance, cfg: TwinConfig) -> ShiftRef:
    return ShiftRef(
        date=shift.shift_date,
        code=shift.code,
        start_local=_hhmm(shift.start, cfg),
        end_local=_hhmm(shift.end, cfg),
        start=shift.start,
        end=shift.end,
        working=shift.working,
    )


def next_working_shift(cfg: TwinConfig, shift: ShiftInstance) -> ShiftInstance | None:
    """The first working shift that starts at or after the end of ``shift``."""
    cal = cfg.calendar
    day = shift.shift_date
    for _ in range(31):
        for candidate in cal.shifts_on(day, working_only=True):
            if candidate.start >= shift.end:
                return candidate
        day += timedelta(days=1)
    return None


def _name(obj: Any, lang: Lang) -> str:
    kk = getattr(obj, "name_kk", None)
    if lang == "kk" and kk:
        return str(kk)
    return str(obj.name_ru)


def entity_name(cfg: TwinConfig, code: str, lang: Lang = "ru") -> str:
    """Display name of any plant entity code (equipment, line, area, buffer, product, site)."""
    if code in cfg.equipment:
        return _name(cfg.equipment[code], lang)
    if code in cfg.lines:
        return _name(cfg.lines[code], lang)
    if code in cfg.areas:
        return _name(cfg.areas[code], lang)
    if code in cfg.buffers:
        return _name(cfg.buffers[code], lang)
    if code in cfg.products:
        return cfg.products[code].name
    if code in ("PLANT", cfg.plant.site.code):
        return _name(cfg.plant.site, lang)
    return code


def _latest_kpis(kpis: Sequence[KpiFact]) -> dict[str, KpiFact]:
    best: dict[str, KpiFact] = {}
    for k in kpis:
        cur = best.get(k.line)
        rank = (k.source == "events", k.version)
        if cur is None or rank > (cur.source == "events", cur.version):
            best[k.line] = k
    return best


def _clip_minutes(stop: StopFact, shift: ShiftInstance, now: datetime) -> float:
    if stop.start is None:
        return (stop.duration_s or 0.0) / 60.0
    start = max(ensure_utc(stop.start), shift.start)
    end = min(ensure_utc(stop.end) if stop.end is not None else ensure_utc(now), shift.end)
    return max((end - start).total_seconds(), 0.0) / 60.0


# --------------------------------------------------------------------------- assembly


def _line_rows(
    cfg: TwinConfig, kpis: dict[str, KpiFact], lang: Lang
) -> tuple[list[LineRow], list[LossRow]]:
    lines: list[LineRow] = []
    losses: list[LossRow] = []
    for code in cfg.flow_lines:
        k = kpis.get(code)
        if k is None:
            continue
        line = cfg.lines[code]
        lines.append(
            LineRow(
                code=code,
                name=_name(line, lang),
                pq=k.pq,
                gq=k.gq,
                defects=k.pq - k.gq,
                oee_pct=pct1(k.oee),
                availability_pct=pct1(k.availability),
                effectiveness_pct=pct1(k.effectiveness),
                quality_pct=pct1(k.quality_ratio),
                defect_rate_pct=pct1(k.defect_rate),
                lost_min=round(k.pbt - k.apt),
                failures=k.failures,
                repair_min=None if k.repair_min is None else round(k.repair_min),
            )
        )
        pri_produced_s = (
            k.effectiveness * k.apt * 60.0 if k.effectiveness is not None else k.pri_good_s
        )
        try:
            tm = TimeModel(
                pot=k.pot,
                pdot=min(k.pdot, k.pot),
                adot=k.adot,
                starved=k.adet,
                aust=k.aust,
                microstop=min(k.microstop_min, max(k.apt, 0.0)),
            )
        except ValueError:
            continue
        tree = line_loss_tree(
            line=code,
            ict_seconds=line.ict_seconds,
            time=tm,
            pri_produced_s=pri_produced_s,
            pri_good_s=k.pri_good_s,
        )
        items = [
            (cat, minutes, cars)
            for cat, (minutes, cars) in tree.by_category().items()
            if minutes >= MIN_LOSS_MIN
        ]
        items.sort(key=lambda item: -item[1])
        for cat, minutes, cars in items[:MAX_LOSSES_PER_LINE]:
            losses.append(
                LossRow(
                    line=code,
                    line_name=_name(line, lang),
                    category=str(cat),
                    name=_LOSS_NAMES[lang][cat],
                    minutes=round(minutes),
                    cars=round(cars, 1),
                )
            )
    return lines, losses


def _quality(cfg: TwinConfig, kpis: dict[str, KpiFact], lang: Lang) -> list[AreaQuality]:
    produced: dict[str, int] = defaultdict(int)
    defects: dict[str, int] = defaultdict(int)
    for code, k in kpis.items():
        area_code = cfg.area_of_line(code).code
        produced[area_code] += k.pq
        defects[area_code] += k.pq - k.gq
    out: list[AreaQuality] = []
    for area in cfg.areas.values():
        if area.code not in produced:
            continue
        pq = produced[area.code]
        out.append(
            AreaQuality(
                area=area.code,
                name=_name(area, lang),
                produced=pq,
                defects=defects[area.code],
                defect_rate_pct=round(defects[area.code] / pq * 100.0, 1) if pq else None,
            )
        )
    return out


def _stops(
    cfg: TwinConfig, facts: ShiftFacts, lang: Lang
) -> tuple[list[StopRow], int, int, int, int]:
    rows: list[tuple[float, StopRow]] = []
    unplanned = planned = 0.0
    micro_n, micro_min = 0, 0.0
    for stop in facts.stops:
        minutes = _clip_minutes(stop, facts.shift, facts.now)
        if stop.entity in cfg.lines:
            if stop.microstop:
                micro_n += 1
                micro_min += minutes
            continue
        if stop.entity not in cfg.equipment or stop.microstop or minutes <= 0:
            continue
        if stop.planned:
            planned += minutes
        else:
            unplanned += minutes
        reason = cfg.reasons.get(stop.reason_code)
        start = max(ensure_utc(stop.start), facts.shift.start) if stop.start else None
        end = min(ensure_utc(stop.end), facts.shift.end) if stop.end else None
        rows.append(
            (
                minutes,
                StopRow(
                    equipment=stop.entity,
                    name=entity_name(cfg, stop.entity, lang),
                    line_name=entity_name(cfg, stop.line, lang),
                    reason_code=stop.reason_code,
                    reason=_name(reason, lang) if reason is not None else stop.reason_code,
                    start_local=_hhmm(start, cfg) if start else "",
                    end_local=_hhmm(end, cfg) if end else None,
                    minutes=round(minutes),
                    planned=stop.planned,
                    open=stop.end is None,
                ),
            )
        )
    rows.sort(key=lambda r: (r[1].planned, -r[0], r[1].equipment))  # unplanned first
    return (
        [r for _, r in rows[:MAX_STOPS] if r.minutes > 0],
        round(unplanned),
        round(planned),
        micro_n,
        round(micro_min),
    )


def _defects(cfg: TwinConfig, facts: ShiftFacts, lang: Lang) -> list[DefectRow]:
    qty: dict[tuple[str, str], int] = defaultdict(int)
    for d in facts.defects:
        qty[(d.area, d.defect_code)] += d.qty
    rows: list[DefectRow] = []
    for (area, code), n in qty.items():
        if n <= 0:
            continue
        defect = cfg.defects.get(code)
        area_cfg = cfg.areas.get(area)
        rows.append(
            DefectRow(
                area=area,
                area_name=_name(area_cfg, lang) if area_cfg is not None else area,
                code=code,
                name=_name(defect, lang) if defect is not None else code,
                qty=n,
            )
        )
    rows.sort(key=lambda r: (-r.qty, r.area, r.code))
    return rows[:MAX_DEFECTS]


_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _alerts(cfg: TwinConfig, facts: ShiftFacts, lang: Lang) -> list[AlertRow]:
    rows: list[AlertRow] = []
    for a in sorted(facts.alerts, key=lambda a: (_SEVERITY_ORDER.get(a.severity, 3), a.ts)):
        rule = cfg.alert_rules.get(a.rule_id)
        title = a.title_kk if lang == "kk" and a.title_kk else a.title_ru
        if lang == "kk" and not a.title_kk and rule is not None and rule.name_kk:
            title = rule.name_kk
        rows.append(
            AlertRow(
                rule_id=a.rule_id,
                severity=a.severity,
                entity=a.entity,
                name=entity_name(cfg, a.entity, lang),
                title=title,
                message=a.message_kk if lang == "kk" and a.message_kk else a.message_ru,
                status=a.status,
                time_local=_hhmm(a.ts, cfg),
            )
        )
    return rows[:MAX_ALERTS]


def _forecast(cfg: TwinConfig, result: ForecastResult | None) -> ForecastSummary | None:
    if result is None:
        return None
    local = to_plant_tz(result.as_of, cfg.timezone)
    plan = result.targets.get("line_plan")
    target = result.targets.get("plant_target", cfg.rules.thresholds.plant_target_per_month)
    p_plan = result.p_reach.get("line_plan")
    rate_plan = result.required_rate.get("line_plan")
    rate_target = result.required_rate.get("plant_target")
    return ForecastSummary(
        month=result.month,
        as_of_date=local.date(),
        as_of_time=local.strftime("%H:%M"),
        mtd=result.mtd,
        p10=round(result.summary.p10),
        p50=round(result.summary.p50),
        p90=round(result.summary.p90),
        plan=plan,
        target=int(target),
        p_plan_pct=pct1(p_plan),
        p_target_pct=pct1(result.p_reach.get("plant_target", 0.0)) or 0.0,
        required_rate_plan=None if rate_plan is None else round(rate_plan, 1),
        required_rate_target=None if rate_target is None else round(rate_target, 1),
    )


def _deviations(
    cfg: TwinConfig,
    lines: list[LineRow],
    quality: list[AreaQuality],
    totals: Totals,
    forecast: ForecastSummary | None,
    thresholds: Thresholds,
    lang: Lang,
) -> list[Deviation]:
    out: list[Deviation] = []
    for line in lines:
        if line.oee_pct is not None and line.oee_pct < thresholds.oee_target_pct:
            out.append(
                Deviation(
                    kind="oee",
                    entity=line.code,
                    name=line.name,
                    value=line.oee_pct,
                    limit=thresholds.oee_target_pct,
                    gap=round(thresholds.oee_target_pct - line.oee_pct, 1),
                )
            )
    for q in quality:
        if q.defect_rate_pct is not None and q.defect_rate_pct > thresholds.defect_rate_limit_pct:
            out.append(
                Deviation(
                    kind="defect_rate",
                    entity=q.area,
                    name=q.name,
                    value=q.defect_rate_pct,
                    limit=thresholds.defect_rate_limit_pct,
                    gap=round(q.defect_rate_pct - thresholds.defect_rate_limit_pct, 1),
                )
            )
    if totals.shortfall:
        last = cfg.flow_lines[-1]
        out.append(
            Deviation(
                kind="plan",
                entity=last,
                name=next((ln.name for ln in lines if ln.code == last), last),
                value=float(totals.output or 0),
                limit=float(totals.plan or 0),
                gap=float(totals.shortfall),
            )
        )
    warn = cfg.rules.thresholds.plan_risk_warn_p * 100.0
    if forecast is not None and forecast.p_plan_pct is not None and forecast.p_plan_pct < warn:
        out.append(
            Deviation(
                kind="forecast",
                entity="PLANT",
                name=_name(cfg.plant.site, lang),
                value=forecast.p_plan_pct,
                limit=round(warn, 1),
            )
        )
    return out


def build_shift_input(cfg: TwinConfig, facts: ShiftFacts, *, lang: Lang = "ru") -> ShiftInput:
    """The report input for one shift (see the module docstring)."""
    kpis = _latest_kpis(facts.kpis)
    lines, losses = _line_rows(cfg, kpis, lang)
    quality = _quality(cfg, kpis, lang)
    stops, unplanned, planned, micro_n, micro_min = _stops(cfg, facts, lang)
    defects = _defects(cfg, facts, lang)
    alerts = _alerts(cfg, facts, lang)
    last = cfg.flow_lines[-1]
    output = kpis[last].gq if last in kpis else None
    plan = cfg.lines[last].plan_rate_per_shift if facts.shift.working else None
    shortfall = plan - output if plan is not None and output is not None else None
    t = cfg.rules.thresholds
    thresholds = Thresholds(
        oee_target_pct=round(t.oee_target * 100.0, 1),
        defect_rate_limit_pct=round(t.defect_rate_limit * 100.0, 1),
    )
    totals = Totals(
        output=output,
        plan=plan,
        attainment_pct=round(output / plan * 100.0, 1) if output is not None and plan else None,
        shortfall=shortfall if shortfall is not None and shortfall > 0 else None,
        unplanned_downtime_min=unplanned,
        planned_downtime_min=planned,
        microstops=micro_n,
        microstop_min=micro_min,
        defects=sum(line.defects for line in lines),
        open_alerts=sum(1 for a in facts.alerts if a.status == "open"),
    )
    bottleneck = [
        BottleneckRow(
            line=b.line,
            name=entity_name(cfg, b.line, lang),
            sole_pct=pct1(b.sole_share) or 0.0,
            shifting_pct=pct1(b.shifting_share) or 0.0,
        )
        for b in sorted(facts.bottleneck, key=lambda b: -(b.sole_share + b.shifting_share))
        if b.sole_share + b.shifting_share > 0
    ]
    forecast = _forecast(cfg, facts.forecast)
    nxt = next_working_shift(cfg, facts.shift)
    source: Literal["events", "import", "none"] = "none"
    if kpis:
        source = "events" if any(k.source == "events" for k in kpis.values()) else "import"
    return ShiftInput(
        lang=lang,
        site=_name(cfg.plant.site, lang),
        shift=_ref(facts.shift, cfg),
        next_shift=_ref(nxt, cfg) if nxt is not None else None,
        source=source,
        closed=bool(kpis) and all(k.final for k in kpis.values()),
        thresholds=thresholds,
        totals=totals,
        lines=lines,
        quality=quality,
        losses=losses,
        stops=stops,
        defects=defects,
        alerts=alerts,
        bottleneck=bottleneck,
        forecast=forecast,
        deviations=_deviations(cfg, lines, quality, totals, forecast, thresholds, lang),
        notes=list(facts.notes),
    )


__all__ = [
    "MAX_ALERTS",
    "MAX_DEFECTS",
    "MAX_STOPS",
    "AlertFact",
    "BottleneckFact",
    "DefectFact",
    "KpiFact",
    "ShiftFacts",
    "StopFact",
    "build_shift_input",
    "entity_name",
    "next_working_shift",
    "pct1",
]
