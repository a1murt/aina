"""Shift report service (SPEC §11.5, US-7).

``create``: facts of the shift (DB) + month forecast (M6 service, current month only) ->
:class:`ShiftInput` -> text:

1. the template text is always built (and checked) first — it is the fallback;
2. with an available LLM provider: one request with the strict prompt; the answer is accepted
   when every number/date/time is in the input, all four sections are present and it has at
   most 250 words; otherwise one regeneration with the list of problems; otherwise — or on any
   provider error (offline, timeout, refusal, network) — the template (``generated_by=template``);
3. the row goes to ``report`` (text, ``generated_by``, model, ``numbers_verified``, input with
   the generation log) with an ``audit_log`` entry ``report.create``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from qost_api.auth import Principal
from qost_api.llm import LlmError, LlmProvider, LlmRequest, UserTurn
from qost_api.reports.data import ReportBackend, ReportRecord, StoredReport
from qost_api.reports.prompt import clean_text, retry_prompt, system_prompt, user_prompt
from twin_core.calendar import ShiftInstance
from twin_core.clock import Clock, ensure_utc
from twin_core.config import TwinConfig
from twin_core.forecast.calibration import month_of
from twin_core.forecast.result import ForecastResult
from twin_core.report import (
    KK_DRAFT,
    MAX_WORDS,
    Lang,
    ShiftFacts,
    ShiftInput,
    build_shift_input,
    has_sections,
    render_template,
    verify_report,
    word_count,
)

log = structlog.get_logger("qost_api.reports")

KIND_SHIFT = "shift"
MAX_ATTEMPTS = 2
"""One LLM answer plus one regeneration (SPEC §11.5)."""

ForecastFn = Callable[[str, Principal], Awaitable[ForecastResult | None]]


class ShiftReportRequest(BaseModel):
    """``POST /api/v1/reports/shift``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    date: date
    shift: str = Field(min_length=1, max_length=8)
    lang: Lang = "ru"
    mode: Literal["auto", "template"] = "auto"
    """``template`` — skip the LLM even when a provider is configured."""


class ReportView(BaseModel):
    id: int
    kind: str
    shift_date: date
    shift_code: str
    lang: str
    text: str
    generated_by: str
    model: str | None
    numbers_verified: bool
    created_ts: datetime
    translation: Literal["final", "draft"]
    """``draft`` for Kazakh texts until a native speaker reviews them."""
    input: dict[str, Any]


class ReportError(Exception):
    def __init__(self, status: int, slug: str, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail


@dataclass(slots=True)
class Generated:
    text: str
    generated_by: Literal["llm", "template"]
    model: str | None
    numbers_verified: bool
    log: dict[str, Any] = field(default_factory=dict)


class ReportService:
    def __init__(
        self,
        cfg: TwinConfig,
        clock: Clock,
        backend: ReportBackend,
        provider: LlmProvider,
        forecast: ForecastFn | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.backend = backend
        self.provider = provider
        self.forecast = forecast

    # ------------------------------------------------------------------ helpers

    def _shift(self, day: date, code: str) -> ShiftInstance:
        try:
            return self.cfg.calendar.shift(day, code)
        except KeyError as exc:
            codes = ", ".join(self.cfg.calendar.shift_codes)
            raise ReportError(
                422, "validation", "Request validation failed", f"unknown shift {code!r} ({codes})"
            ) from exc

    async def _forecast(
        self, shift: ShiftInstance, now: datetime, principal: Principal
    ) -> tuple[ForecastResult | None, list[str]]:
        if self.forecast is None:
            return None, ["прогноз месяца недоступен"]
        month = shift.shift_date.isoformat()[:7]
        if month != month_of(self.cfg, now):
            return None, [f"прогноз месяца {month} не строится: месяц уже не текущий"]
        try:
            return await self.forecast(month, principal), []
        except Exception as exc:  # the report must not fail because of the forecast
            log.warning("report_forecast_failed", error=str(exc)[:200])
            return None, ["прогноз месяца недоступен"]

    # ------------------------------------------------------------------ use cases

    async def build_input(
        self, day: date, code: str, lang: Lang, principal: Principal
    ) -> ShiftInput:
        shift = self._shift(day, code)
        now = ensure_utc(self.clock.now())
        if shift.start > now:
            raise ReportError(
                422,
                "shift-not-started",
                "Shift has not started",
                f"shift {day.isoformat()}/{code} starts at {shift.start.isoformat()}",
            )
        facts = await self.backend.shift_facts(self.cfg, shift, now=now)
        if not facts.kpis:
            if now < shift.end:
                raise ReportError(
                    409,
                    "shift-open",
                    "Shift is still running",
                    "the report is built after the engine closes the shift",
                )
            raise ReportError(
                404,
                "no-shift-data",
                "No data for this shift",
                f"no KPI rows for {day.isoformat()}/{code}",
            )
        forecast, notes = await self._forecast(shift, now, principal)
        facts = ShiftFacts(
            shift=facts.shift,
            now=facts.now,
            kpis=facts.kpis,
            stops=facts.stops,
            defects=facts.defects,
            alerts=facts.alerts,
            bottleneck=facts.bottleneck,
            forecast=forecast,
            notes=(*facts.notes, *notes),
        )
        return build_shift_input(self.cfg, facts, lang=lang)

    async def generate(self, data: ShiftInput, *, use_llm: bool = True) -> Generated:
        template = render_template(data)
        template_ok = verify_report(template, data).ok
        meta: dict[str, Any] = {
            "provider": self.provider.name,
            "model": self.provider.model,
            "attempts": [],
            "fallback": None,
        }
        fallback = Generated(template, "template", None, template_ok, meta)
        if not use_llm:
            meta["fallback"] = "mode=template"
            return fallback
        if not self.provider.available:
            meta["fallback"] = self.provider.reason or "provider unavailable"
            return fallback
        previous, problems = "", list[str]()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = user_prompt(data) if attempt == 1 else retry_prompt(data, previous, problems)
            request = LlmRequest(system=system_prompt(data.lang), messages=(UserTurn(prompt),))
            try:
                response = await self.provider.complete(request)
            except LlmError as exc:
                meta["fallback"] = f"{exc.kind}: {exc}"
                log.warning("report_llm_failed", kind=exc.kind, error=str(exc)[:200])
                return fallback
            previous = clean_text(response.text)
            check = verify_report(previous, data)
            problems = [m.describe() for m in check.mismatches]
            if not has_sections(previous, data.lang):
                problems.append("нет одного из четырёх разделов или нарушен их порядок")
            words = word_count(previous)
            if words > MAX_WORDS:
                problems.append(f"слишком длинно: {words} слов, нужно не больше {MAX_WORDS}")
            if response.stop_reason == "max_tokens":
                problems.append("ответ обрезан")
            meta["attempts"].append(
                {
                    "n": attempt,
                    "model": response.model,
                    "ok": not problems,
                    "problems": problems,
                    "numbers_checked": check.checked,
                    "usage": response.usage,
                }
            )
            if not problems:
                meta["model"] = response.model
                return Generated(previous, "llm", response.model, True, meta)
        meta["fallback"] = "numbers not verified after regeneration"
        return fallback

    async def create(self, request: ShiftReportRequest, principal: Principal) -> ReportView:
        data = await self.build_input(request.date, request.shift, request.lang, principal)
        generated = await self.generate(data, use_llm=request.mode == "auto")
        record = ReportRecord(
            kind=KIND_SHIFT,
            shift_date=request.date,
            shift_code=request.shift,
            lang=request.lang,
            text=generated.text,
            generated_by=generated.generated_by,
            model=generated.model,
            numbers_verified=generated.numbers_verified,
            input={"shift": data.model_dump(mode="json"), "generation": generated.log},
            created_ts=self.clock.now(),
        )
        report_id = await self.backend.save(record, principal=principal)
        log.info(
            "report_created",
            id=report_id,
            shift=f"{request.date.isoformat()}/{request.shift}",
            lang=request.lang,
            generated_by=generated.generated_by,
            numbers_verified=generated.numbers_verified,
        )
        return view(StoredReport(report_id, record))

    async def find(self, day: date, code: str, lang: Lang | None) -> list[ReportView]:
        self._shift(day, code)
        return [view(r) for r in await self.backend.find(KIND_SHIFT, day, code, lang)]


def view(stored: StoredReport) -> ReportView:
    r = stored.record
    return ReportView(
        id=stored.id,
        kind=r.kind,
        shift_date=r.shift_date,
        shift_code=r.shift_code,
        lang=r.lang,
        text=r.text,
        generated_by=r.generated_by,
        model=r.model,
        numbers_verified=r.numbers_verified,
        created_ts=r.created_ts,
        translation="draft" if r.lang == "kk" and KK_DRAFT else "final",
        input=r.input,
    )


__all__ = [
    "KIND_SHIFT",
    "MAX_ATTEMPTS",
    "ForecastFn",
    "Generated",
    "ReportError",
    "ReportService",
    "ReportView",
    "ShiftReportRequest",
    "view",
]
