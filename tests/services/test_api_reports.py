"""T-LLM: shift report service and ``/api/v1/reports/shift`` without a database (in-memory
backend, fake LLM): template path, LLM path, regeneration, fallback to the template, offline
guard, roles and RFC 7807 problems. Persistence: tests/integration/test_reports_db.py."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from qost_api.app import create_app
from qost_api.auth import Principal
from qost_api.llm import (
    LlmProvider,
    LlmRequest,
    LlmResponse,
    LlmSettings,
    LlmTimeoutError,
    NoneProvider,
    create_provider,
)
from qost_api.reports.data import ReportRecord, StoredReport
from qost_api.reports.service import ReportService, ShiftReportRequest
from qost_api.routes.reports import get_report_service
from report_support import DAY, sample_facts, sample_forecast
from twin_core.calendar import ShiftInstance
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.forecast.result import ForecastResult
from twin_core.report import ShiftFacts, render_template

MASTER = Principal("master1", "master")
PROBLEM = "application/problem+json"


@dataclass
class MemoryBackend:
    facts: ShiftFacts | None
    saved: list[tuple[ReportRecord, Principal]] = field(default_factory=list)

    async def shift_facts(
        self, cfg: TwinConfig, shift: ShiftInstance, *, now: datetime
    ) -> ShiftFacts:
        if self.facts is not None and self.facts.shift == shift:
            return self.facts
        return ShiftFacts(shift=shift, now=now)

    async def save(self, record: ReportRecord, *, principal: Principal) -> int:
        self.saved.append((record, principal))
        return len(self.saved)

    async def find(
        self, kind: str, shift_date: date, shift_code: str, lang: str | None
    ) -> list[StoredReport]:
        found = [
            StoredReport(n + 1, r)
            for n, (r, _) in enumerate(self.saved)
            if r.kind == kind
            and r.shift_date == shift_date
            and r.shift_code == shift_code
            and (lang is None or r.lang == lang)
        ]
        return found[::-1]


class ScriptedLlm:
    """Answers from a script; records the prompts."""

    name = "fake"
    model: str | None = "fake-model"

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return None

    async def complete(self, request: LlmRequest) -> LlmResponse:
        turn = request.messages[-1]
        self.prompts.append(getattr(turn, "text", ""))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LlmResponse(
            answer, (), "end_turn", "fake-model-served", "fake", {"output_tokens": 1}
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def shift(cfg: TwinConfig) -> ShiftInstance:
    return cfg.calendar.shift(DAY, "A")


@pytest.fixture
def clock(shift: ShiftInstance) -> ManualClock:
    return ManualClock(shift.end + timedelta(minutes=10))


@pytest.fixture
def backend(cfg: TwinConfig, shift: ShiftInstance) -> MemoryBackend:
    return MemoryBackend(sample_facts(cfg, shift))


def service(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend, provider: LlmProvider
) -> ReportService:
    async def outlook(month: str, principal: Principal) -> ForecastResult | None:
        return sample_forecast(clock.now())

    return ReportService(cfg, clock, backend, provider, outlook)


def request(lang: str = "ru") -> ShiftReportRequest:
    return ShiftReportRequest.model_validate({"date": "2026-10-15", "shift": "A", "lang": lang})


async def llm_text(svc: ReportService, lang: str = "ru") -> str:
    """A valid LLM-style answer: the template text with Markdown noise around it."""
    data = await svc.build_input(DAY, "A", lang, MASTER)  # type: ignore[arg-type]
    return "**" + render_template(data).replace("Итоги", "## Итоги") + "**"


async def test_provider_none_gives_the_verified_template(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    view = await service(cfg, clock, backend, NoneProvider()).create(request(), MASTER)
    assert view.generated_by == "template"
    assert view.model is None
    assert view.numbers_verified
    assert view.translation == "final"
    assert view.text.startswith("Сменный рапорт:")
    generation = view.input["generation"]
    assert generation["fallback"] == "LLM_PROVIDER=none"
    assert generation["attempts"] == []
    assert view.input["shift"]["forecast"]["p50"] == 4787
    record, who = backend.saved[0]
    assert who == MASTER
    assert record.created_ts == clock.now()


async def test_llm_answer_with_verified_numbers_is_kept(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    llm = ScriptedLlm()
    svc = service(cfg, clock, backend, llm)
    llm.answers.append(await llm_text(svc))
    view = await svc.create(request(), MASTER)
    assert view.generated_by == "llm"
    assert view.numbers_verified
    assert view.model == "fake-model-served"
    assert "**" not in view.text
    assert "## " not in view.text
    assert len(llm.prompts) == 1
    assert '"output":103' in llm.prompts[0]


async def test_wrong_number_triggers_one_regeneration(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    llm = ScriptedLlm()
    svc = service(cfg, clock, backend, llm)
    good = await llm_text(svc)
    llm.answers += [good.replace("Выпуск 103 авто", "Выпуск 113 авто"), good]
    view = await svc.create(request(), MASTER)
    assert view.generated_by == "llm"
    assert view.numbers_verified
    attempts = view.input["generation"]["attempts"]
    assert [a["ok"] for a in attempts] == [False, True]
    assert attempts[0]["problems"] == ["«113» — нет во входных данных"]
    assert "«113» — нет во входных данных" in llm.prompts[1]
    assert "Выпуск 113 авто" in llm.prompts[1]  # the previous draft is quoted


async def test_two_bad_answers_fall_back_to_the_template(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    llm = ScriptedLlm()
    svc = service(cfg, clock, backend, llm)
    good = await llm_text(svc)
    llm.answers += [good.replace("55 мин", "56 мин"), "Итоги\nВсё хорошо."]
    view = await svc.create(request(), MASTER)
    assert view.generated_by == "template"
    assert view.numbers_verified
    generation = view.input["generation"]
    assert generation["fallback"] == "numbers not verified after regeneration"
    assert len(generation["attempts"]) == 2
    assert any("раздел" in p for p in generation["attempts"][1]["problems"])
    assert view.text == render_template(await svc.build_input(DAY, "A", "ru", MASTER))


async def test_llm_errors_fall_back_to_the_template(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    llm = ScriptedLlm(LlmTimeoutError("no answer within 20 s"))
    view = await service(cfg, clock, backend, llm).create(request("kk"), MASTER)
    assert view.generated_by == "template"
    assert view.translation == "draft"
    assert view.input["generation"]["fallback"] == "timeout: no answer within 20 s"
    assert view.text.startswith("Ауысым есебі:")


async def test_offline_anthropic_never_reaches_the_network(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        pytest.fail(f"network call under OFFLINE: {request.url}")

    provider = create_provider(
        LlmSettings(llm_provider="anthropic", offline=True, anthropic_api_key=SecretStr("sk-test")),
        transport=httpx2.MockTransport(handler),
    )
    view = await service(cfg, clock, backend, provider).create(request(), MASTER)
    assert view.generated_by == "template"
    assert view.numbers_verified
    assert "OFFLINE" in view.input["generation"]["fallback"]


async def test_template_mode_skips_the_llm(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    llm = ScriptedLlm()
    body = ShiftReportRequest.model_validate(
        {"date": "2026-10-15", "shift": "A", "mode": "template"}
    )
    view = await service(cfg, clock, backend, llm).create(body, MASTER)
    assert view.generated_by == "template"
    assert llm.prompts == []


async def test_forecast_failure_does_not_break_the_report(
    cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend
) -> None:
    async def broken(month: str, principal: Principal) -> ForecastResult | None:
        raise RuntimeError("no calibration data")

    svc = ReportService(cfg, clock, backend, NoneProvider(), broken)
    view = await svc.create(request(), MASTER)
    assert view.input["shift"]["forecast"] is None
    assert view.input["shift"]["notes"] == ["прогноз месяца недоступен"]


# ---------------------------------------------------------------------------- HTTP


@pytest.fixture
def client(cfg: TwinConfig, clock: ManualClock, backend: MemoryBackend) -> Iterator[TestClient]:
    app = create_app(cfg, clock=clock, database_url=None)
    svc = service(cfg, clock, backend, NoneProvider())
    app.dependency_overrides[get_report_service] = lambda: svc
    with TestClient(app) as test_client:
        yield test_client


def test_post_and_get_shift_report(client: TestClient) -> None:
    body = {"date": "2026-10-15", "shift": "A", "lang": "ru"}
    created = client.post("/api/v1/reports/shift", json=body, headers={"X-Dev-Role": "master"})
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["generated_by"] == "template"
    assert report["numbers_verified"] is True
    assert report["shift_date"] == "2026-10-15"
    assert report["shift_code"] == "A"
    got = client.get(
        "/api/v1/reports/shift",
        params={"date": "2026-10-15", "shift": "A"},
        headers={"X-Dev-Role": "director"},
    )
    assert got.status_code == 200
    assert [r["id"] for r in got.json()] == [report["id"]]
    assert (
        client.get(
            "/api/v1/reports/shift",
            params={"date": "2026-10-15", "shift": "A", "lang": "kk"},
            headers={"X-Dev-Role": "director"},
        ).json()
        == []
    )


@pytest.mark.parametrize("role", ["operator", "maintenance", "quality"])
def test_report_roles(client: TestClient, role: str) -> None:
    body = {"date": "2026-10-15", "shift": "A"}
    response = client.post("/api/v1/reports/shift", json=body, headers={"X-Dev-Role": role})
    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM


@pytest.mark.parametrize(
    ("body", "status", "slug"),
    [
        ({"date": "2026-10-15", "shift": "Z"}, 422, "validation"),
        ({"date": "2026-10-16", "shift": "A"}, 422, "shift-not-started"),
        ({"date": "2026-10-15", "shift": "B"}, 409, "shift-open"),
        ({"date": "2026-10-14", "shift": "A"}, 404, "no-shift-data"),
        ({"date": "2026-10-15", "shift": "A", "lang": "en"}, 422, "validation"),
    ],
)
def test_report_problems(
    client: TestClient, clock: ManualClock, body: dict[str, str], status: int, slug: str
) -> None:
    clock.set(datetime.fromisoformat("2026-10-15T16:00:00+05:00"))
    response = client.post("/api/v1/reports/shift", json=body, headers={"X-Dev-Role": "master"})
    assert response.status_code == status, response.text
    assert response.json()["type"] == f"/problems/{slug}"


def test_without_database_reports_are_unavailable(cfg: TwinConfig) -> None:
    with TestClient(create_app(cfg, database_url=None)) as c:
        response = c.post(
            "/api/v1/reports/shift",
            json={"date": "2026-10-15", "shift": "A"},
            headers={"X-Dev-Role": "master"},
        )
    assert response.status_code == 503
    assert response.json()["type"] == "/problems/no-database"
