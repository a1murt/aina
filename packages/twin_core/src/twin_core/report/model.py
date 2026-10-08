"""Structured input of a shift report (SPEC §11.5): what the template and the LLM may say.

All numbers are rounded the way a report shows them (percent — 1 decimal, minutes — whole,
cars — 1 decimal), so the text can quote them verbatim and the number check (``numbers``)
compares against exactly these values. Names are in the report language (kk falls back to ru).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Lang = Literal["ru", "kk"]
LANGS: tuple[Lang, ...] = ("ru", "kk")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ShiftRef(_Frozen):
    date: date
    code: str
    start_local: str
    """Plant-local ``HH:MM``."""
    end_local: str
    start: datetime
    end: datetime
    working: bool


class Thresholds(_Frozen):
    oee_target_pct: float
    defect_rate_limit_pct: float


class Totals(_Frozen):
    output: int | None
    """Good cars from the last flow line in the shift (as the engine counts the month)."""
    plan: int | None
    attainment_pct: float | None
    shortfall: int | None
    """``plan − output`` when positive."""
    unplanned_downtime_min: int
    planned_downtime_min: int
    microstops: int
    microstop_min: int
    defects: int
    open_alerts: int


class LineRow(_Frozen):
    code: str
    name: str
    pq: int
    gq: int
    defects: int
    oee_pct: float | None
    availability_pct: float | None
    effectiveness_pct: float | None
    quality_pct: float | None
    defect_rate_pct: float | None
    lost_min: int
    """PBT − APT: time the line did not run against its planned busy time."""
    failures: int | None
    repair_min: int | None


class AreaQuality(_Frozen):
    area: str
    name: str
    produced: int
    defects: int
    defect_rate_pct: float | None


class LossRow(_Frozen):
    line: str
    line_name: str
    category: str
    name: str
    minutes: int
    cars: float


class StopRow(_Frozen):
    equipment: str
    name: str
    line_name: str
    reason_code: str
    reason: str
    start_local: str
    end_local: str | None
    minutes: int
    planned: bool
    open: bool


class DefectRow(_Frozen):
    area: str
    area_name: str
    code: str
    name: str
    qty: int


class AlertRow(_Frozen):
    rule_id: str
    severity: str
    entity: str
    name: str
    title: str
    message: str
    status: str
    time_local: str


class BottleneckRow(_Frozen):
    line: str
    name: str
    sole_pct: float
    shifting_pct: float


class ForecastSummary(_Frozen):
    month: str
    as_of_date: date
    as_of_time: str
    mtd: int
    p10: int
    p50: int
    p90: int
    plan: int | None
    target: int
    p_plan_pct: float | None
    p_target_pct: float
    required_rate_plan: float | None
    required_rate_target: float | None


class Deviation(_Frozen):
    kind: Literal["oee", "defect_rate", "plan", "forecast"]
    entity: str
    name: str
    value: float
    limit: float
    gap: float | None = None


class ShiftInput(_Frozen):
    version: Literal[1] = 1
    lang: Lang
    site: str
    shift: ShiftRef
    next_shift: ShiftRef | None
    source: Literal["events", "import", "none"]
    closed: bool
    """KPIs of the shift are final (the engine closed it, or it was imported)."""
    thresholds: Thresholds
    totals: Totals
    lines: list[LineRow]
    quality: list[AreaQuality]
    losses: list[LossRow]
    stops: list[StopRow]
    defects: list[DefectRow]
    alerts: list[AlertRow]
    bottleneck: list[BottleneckRow]
    forecast: ForecastSummary | None
    deviations: list[Deviation]
    notes: list[str]

    def names(self) -> list[str]:
        """Entity names and codes of the input (masked by the number check)."""
        out: set[str] = {self.site}
        for line in self.lines:
            out.update((line.code, line.name))
        for q in self.quality:
            out.update((q.area, q.name))
        for loss in self.losses:
            out.update((loss.line, loss.line_name, loss.name))
        for s in self.stops:
            out.update((s.equipment, s.name, s.line_name, s.reason_code, s.reason))
        for d in self.defects:
            out.update((d.area, d.area_name, d.code, d.name))
        for a in self.alerts:
            out.update((a.rule_id, a.entity, a.name, a.title))
        for b in self.bottleneck:
            out.update((b.line, b.name))
        for dev in self.deviations:
            out.update((dev.entity, dev.name))
        return sorted(out)


__all__ = [
    "LANGS",
    "AlertRow",
    "AreaQuality",
    "BottleneckRow",
    "DefectRow",
    "Deviation",
    "ForecastSummary",
    "Lang",
    "LineRow",
    "LossRow",
    "ShiftInput",
    "ShiftRef",
    "StopRow",
    "Thresholds",
    "Totals",
]
