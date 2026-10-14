"""Template shift reports for the last closed shifts (``python -m qost_api seed-reports``).

``make demo`` replays the history but nobody closes those shifts by hand, so the director's
"latest shift report" block and ``/reports`` would start empty (WIN-9). This fills the last
``N`` closed working shifts with template reports (``mode=template``, no LLM) written by the
internal ``system`` principal; every report gets its ``report.create`` audit entry. A shift that
already has a report in the language is skipped, so a second run writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from qost_api.auth import Principal
from qost_api.reports.service import ReportError, ReportService, ShiftReportRequest
from twin_core.calendar import ShiftInstance
from twin_core.clock import ensure_utc, to_plant_tz
from twin_core.config import TwinConfig
from twin_core.report import Lang

SYSTEM_PRINCIPAL = Principal(username="system", role="admin", user_id=None, display_name="Система")
LOOKBACK = timedelta(days=14)
"""How far back closed working shifts are looked for (covers long weekends and holidays)."""


@dataclass(frozen=True, slots=True)
class SeededReport:
    date: str
    shift: str
    status: str
    """``created`` | ``exists`` | ``skipped: <reason>``."""
    report_id: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "date": self.date,
            "shift": self.shift,
            "status": self.status,
            "report_id": self.report_id,
        }


def closed_shifts(cfg: TwinConfig, now: datetime, last: int) -> list[ShiftInstance]:
    """The last ``last`` working shifts that ended at or before ``now``, oldest first."""
    now = ensure_utc(now)
    today = to_plant_tz(now, cfg.timezone).date()
    shifts = [
        s
        for s in cfg.calendar.materialize((to_plant_tz(now - LOOKBACK, cfg.timezone)).date(), today)
        if s.working and s.end <= now
    ]
    return shifts[-last:] if last > 0 else []


async def seed_reports(
    service: ReportService,
    cfg: TwinConfig,
    *,
    now: datetime,
    last: int,
    lang: Lang = "ru",
    principal: Principal = SYSTEM_PRINCIPAL,
) -> list[SeededReport]:
    """Create the missing template reports of the last ``last`` closed shifts."""
    out: list[SeededReport] = []
    for shift in closed_shifts(cfg, now, last):
        day, code = shift.shift_date, shift.code
        existing = await service.find(day, code, lang)
        if existing:
            out.append(SeededReport(day.isoformat(), code, "exists", existing[0].id))
            continue
        request = ShiftReportRequest(date=day, shift=code, lang=lang, mode="template")
        try:
            view = await service.create(request, principal)
        except ReportError as exc:  # e.g. no KPI rows for a shift: report it, go on
            out.append(SeededReport(day.isoformat(), code, f"skipped: {exc.slug}"))
            continue
        out.append(SeededReport(day.isoformat(), code, "created", view.id))
    return out


__all__ = ["SYSTEM_PRINCIPAL", "SeededReport", "closed_shifts", "seed_reports"]
