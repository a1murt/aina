"""T-LLM (copilot, SPEC §11.5): the tool loop with a fake provider (tool calls, the ≤ 5 limit,
role filtering, refusal), the deterministic offline answers, the request log and the endpoint."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from api_support import FakeSession, bearer
from qost_api.app import create_app
from qost_api.auth import Principal
from qost_api.copilot.offline import find_entities, find_period
from qost_api.copilot.service import MAX_TOOL_CALLS, CopilotService
from qost_api.copilot.tools import TOOLS, Toolbox, compact, tools_for_role
from qost_api.db import get_session
from qost_api.llm import (
    LlmRequest,
    LlmResponse,
    LlmTimeoutError,
    NoneProvider,
    ToolCall,
    ToolResultsTurn,
)
from qost_api.routes.copilot import get_copilot_service
from qost_api.routes.work_orders import prefill_from_alert
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig

PRINCIPAL = {
    role: Principal(username=role, role=role, user_id=1)
    for role in ("director", "master", "quality", "maintenance")
}


class FakeProvider:
    """Plays a script of responses; remembers every request."""

    name = "fake"
    model = "fake-model"
    reason = None

    def __init__(self, script: list[LlmResponse | Exception]) -> None:
        self.script = deque(script)
        self.requests: list[LlmRequest] = []

    @property
    def available(self) -> bool:
        return True

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        item = self.script.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        return None


def tools(*calls: tuple[str, dict[str, Any]]) -> LlmResponse:
    return LlmResponse(
        "",
        tuple(ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(calls)),
        "tool_use",
        "fake-model",
        "fake",
    )


def answer(text: str) -> LlmResponse:
    return LlmResponse(text, (), "end_turn", "fake-model", "fake")


KPI = {
    "level": "line",
    "code": "PAINT-1",
    "from": "2026-10-01",
    "to": "2026-10-16",
    "granularity": "month",
    "items": [
        {
            "level": "line",
            "code": "PAINT-1",
            "month": "2026-10",
            "oee": 0.8123,
            "availability": 0.9,
            "effectiveness": 0.95,
            "quality_ratio": 0.95,
            "pq": 1800,
            "gq": 1710,
            "defect_rate": 0.05,
            "failures": 3,
            "mtbf_h": 41.2,
        }
    ],
}
ALERTS = {
    "items": [
        {
            "id": 7,
            "ts": "2026-10-16T05:00:00+00:00",
            "rule_id": "AL-M1",
            "severity": "warning",
            "entity": "ABB-04",
            "status": "open",
            "message_ru": "Робот ABB-04: риск отказа",
        }
    ],
    "open_counts": {"critical": 0, "warning": 1, "info": 0},
    "more": False,
}
HEALTH = {
    "code": "ABB-04",
    "name_ru": "Робот ABB-04",
    "state": "RUNNING",
    "signals": [],
    "prediction": {
        "horizon_h": 8.0,
        "p_failure": 0.83,
        "health_index": 17.0,
        "factors": ["рост ошибок датчиков"],
    },
    "limits": [],
}


@pytest.fixture
def toolbox_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Replace the data side of the tools; record which tool ran with which arguments."""
    ran: list[tuple[str, dict[str, Any]]] = []

    def fake(name: str, payload: Any) -> Any:
        async def tool(self: Toolbox, a: dict[str, Any]) -> Any:
            ran.append((name, dict(a)))
            return payload

        return tool

    for name, payload in (
        ("get_kpi", KPI),
        ("get_alerts", ALERTS),
        ("get_equipment_health", HEALTH),
        ("get_downtime", {"items": [], "count_shown": 0, "more": False}),
        (
            "get_losses",
            {
                "from": "2026-10-01",
                "to": "2026-10-16",
                "totals": {"minutes": 1, "units": 1, "kzt": 1},
                "categories": [],
            },
        ),
    ):
        monkeypatch.setattr(Toolbox, f"_{name}", fake(name, payload))
    return ran


def service(cfg: TwinConfig, provider: Any) -> CopilotService:
    return CopilotService(cfg, ManualClock(cfg.simulation.clock.demo_start), provider)


async def ask(
    svc: CopilotService, question: str, role: str = "director", session: FakeSession | None = None
) -> Any:
    return await svc.ask(
        question=question,
        principal=PRINCIPAL[role],
        lang="ru",
        request=cast(Any, None),
        session=cast(Any, session or FakeSession()),
    )


# --------------------------------------------------------------------------- LLM loop


async def test_tool_loop_cites_the_period_and_entities_and_logs(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    provider = FakeProvider(
        [
            tools(
                (
                    "get_kpi",
                    {
                        "level": "line",
                        "code": "PAINT-1",
                        "from": "2026-10-01",
                        "to": "2026-10-16",
                        "granularity": "month",
                    },
                ),
                ("get_alerts", {"status": "open"}),
            ),
            answer("OEE окраски-1 — 81,2 %. Открыто одно предупреждение по ABB-04."),
        ]
    )
    session = FakeSession()
    res = await ask(service(cfg, provider), "Какой OEE у Окраски-1 за месяц?", session=session)
    assert res.mode == "llm"
    assert res.model == "fake-model"
    assert not res.refused
    assert [name for name, _ in toolbox_calls] == ["get_kpi", "get_alerts"]
    assert res.answer.startswith("OEE окраски-1 — 81,2 %")
    assert "Основание: период 2026-10-01 … 2026-10-16" in res.answer
    assert res.basis["entities"] == ["PAINT-1", "ABB-04"]
    assert [c["name"] for c in res.tool_calls] == ["get_kpi", "get_alerts"]
    # the second request carries both tool results, compacted JSON the model can read
    results = provider.requests[1].messages[-1]
    assert isinstance(results, ToolResultsTurn)
    assert len(results.results) == 2
    assert json.loads(results.results[0].content)["items"][0]["code"] == "PAINT-1"
    # the request log: one row, 30-day retention against the plant clock
    (row,) = [o for o in session.added if type(o).__name__ == "CopilotLog"]
    assert (row.mode, row.role, row.n_tools if hasattr(row, "n_tools") else 2) == (
        "llm",
        "director",
        2,
    )
    assert row.question.startswith("Какой OEE")
    assert row.tool_calls[0]["name"] == "get_kpi"
    ((_stmt, params),) = [e for e in session.executed if "DELETE FROM copilot_log" in str(e[0])]
    assert params["cutoff"] == ManualClock(cfg.simulation.clock.demo_start).now() - timedelta(
        days=30
    )
    assert session.commits == 1


async def test_at_most_five_tool_calls_per_question(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    seven = tools(*[("get_kpi", {"level": "plant"})] * 7)
    provider = FakeProvider([seven, answer("Итог по пяти вызовам.")])
    res = await ask(service(cfg, provider), "Сравни всё")
    assert MAX_TOOL_CALLS == 5
    assert len(toolbox_calls) == 5
    assert len(res.tool_calls) == 5
    results = provider.requests[1].messages[-1]
    assert isinstance(results, ToolResultsTurn)
    assert [r.is_error for r in results.results] == [False] * 5 + [True] * 2
    assert "limit" in results.results[-1].content


async def test_a_model_that_never_stops_calling_tools_ends_in_the_offline_answer(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    provider = FakeProvider([tools(("get_kpi", {"level": "plant"}))] * 20)
    res = await ask(service(cfg, provider), "Какой OEE у Окраски за месяц?")
    assert res.mode == "offline"
    assert "budget" in (res.fallback_reason or "")
    assert len(provider.requests) == 7  # 5 calls + 2 more rounds, then cut off
    assert res.answer


async def test_tools_depend_on_the_role_and_a_forbidden_call_is_not_run(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    names = {t.name for t in TOOLS}
    assert names == {
        "get_kpi",
        "get_losses",
        "get_downtime",
        "get_defects",
        "get_alerts",
        "get_forecast",
        "get_equipment_health",
        "get_bottleneck",
    }
    assert {t.name for t in tools_for_role("director")} == names
    assert {t.name for t in tools_for_role("quality")} == {"get_kpi", "get_defects", "get_alerts"}
    assert "get_forecast" not in {t.name for t in tools_for_role("master")}
    provider = FakeProvider(
        [
            tools(("get_forecast", {}), ("get_kpi", {"level": "plant"})),
            answer("Прогноз мне недоступен."),
        ]
    )
    res = await ask(service(cfg, provider), "Выполним ли план?", role="quality")
    offered = {t.name for t in provider.requests[0].tools}
    assert offered == {"get_kpi", "get_defects", "get_alerts"}
    assert [n for n, _ in toolbox_calls] == ["get_kpi"]  # get_forecast never ran
    results = provider.requests[1].messages[-1]
    assert isinstance(results, ToolResultsTurn)
    assert results.results[0].is_error
    assert "not available for the role quality" in results.results[0].content
    assert [c["ok"] for c in res.tool_calls] == [False, True]


async def test_bad_arguments_come_back_as_errors_not_exceptions(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    provider = FakeProvider(
        [
            tools(
                ("get_kpi", {"level": "line", "from": "вчера"}),
                ("get_equipment_health", {}),
                ("get_kpi", {"nope": 1}),
                ("drop_tables", {}),
            ),
            answer("Не удалось получить данные."),
        ]
    )
    res = await ask(service(cfg, provider), "kpi")
    assert toolbox_calls == []
    assert [c["ok"] for c in res.tool_calls] == [False] * 4
    errors = [
        json.loads(r.content)["error"]
        for r in cast(ToolResultsTurn, provider.requests[1].messages[-1]).results
    ]
    assert "YYYY-MM-DD" in errors[0]
    assert "missing argument" in errors[1]
    assert "unknown argument" in errors[2]
    assert "not available" in errors[3]
    assert "Основание" not in res.answer  # nothing succeeded: nothing to cite


async def test_off_topic_questions_are_refused_politely(cfg: TwinConfig) -> None:
    provider = FakeProvider(
        [answer("REFUSE: Я отвечаю только на вопросы о работе завода: OEE, простои.")]
    )
    res = await ask(service(cfg, provider), "Напиши стих про осень")
    assert res.refused
    assert res.tool_calls == []
    assert "REFUSE" not in res.answer
    assert res.answer.startswith("Я отвечаю только на вопросы о работе завода")
    assert "Основание" not in res.answer
    system = provider.requests[0].system
    assert "REFUSE:" in system
    assert "OEE" not in system
    assert "ABB-04" in system
    assert "PAINT-1" in system


async def test_provider_failure_falls_back_to_the_offline_answer(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    provider = FakeProvider([LlmTimeoutError("20 s")])
    res = await ask(service(cfg, provider), "Какой OEE у линии Окраска-1 сегодня?")
    assert res.mode == "offline"
    assert "LlmTimeoutError" in (res.fallback_reason or "")
    assert toolbox_calls[0][0] == "get_kpi"
    assert "OEE" in res.answer
    assert "81,23%" in res.answer


# --------------------------------------------------------------------------- offline


async def test_offline_answers_canned_questions_from_the_same_tools(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    svc = service(cfg, NoneProvider())
    res = await ask(svc, "Какой OEE у Окраски-1 за месяц?")
    assert res.mode == "offline"
    assert res.fallback_reason == "LLM_PROVIDER=none"
    assert res.model is None
    assert toolbox_calls[-1][0] == "get_kpi"
    assert toolbox_calls[-1][1]["level"] == "line"
    assert toolbox_calls[-1][1]["code"] == "PAINT-1"
    assert "OEE 81,23%" in res.answer
    assert "Основание: период" in res.answer
    assert "PAINT-1" in res.answer

    res = await ask(svc, "Какова вероятность отказа ABB-04?", role="maintenance")
    assert toolbox_calls[-1] == ("get_equipment_health", {"code": "ABB-04"})
    assert "вероятность отказа в ближайшие 8 ч — 83%" in res.answer
    assert "рост ошибок датчиков" in res.answer

    res = await ask(svc, "Какие сейчас открытые оповещения?", role="master")
    assert toolbox_calls[-1][0] == "get_alerts"  # type: ignore[comparison-overlap]
    assert "ABB-04" in res.answer


async def test_offline_respects_roles_and_refuses_the_rest(
    cfg: TwinConfig, toolbox_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    svc = service(cfg, NoneProvider())
    res = await ask(svc, "Какова вероятность выполнить план месяца?", role="quality")
    assert "недоступен" in res.answer
    assert not res.tool_calls
    res = await ask(svc, "Расскажи анекдот", role="director")
    assert res.refused
    assert not res.tool_calls
    assert "только на вопросы о работе завода" in res.answer
    assert toolbox_calls == []


def test_offline_entity_and_period_parsing(cfg: TwinConfig) -> None:
    e = find_entities(cfg, "Сколько простоев было у Камеры-02 и на сварке в Окраске-1 вчера?")
    assert e.equipment == ["BOOTH-02"]
    assert e.areas == ["WELD"]
    assert e.lines == ["PAINT-1"]
    assert find_entities(cfg, "abb-04 и ABB 01").equipment == ["ABB-04"]
    from datetime import date

    today = date(2026, 10, 16)
    assert find_period("что было вчера", today)[:2] == (date(2026, 10, 15),) * 2
    assert find_period("за неделю", today)[0] == date(2026, 10, 10)
    assert find_period("12.10 смена", today)[:2] == (date(2026, 10, 12),) * 2
    assert find_period("за месяц", today)[0] == date(2026, 10, 1)


def test_compact_stays_within_the_limit_and_rounds() -> None:
    big = {"items": [{"i": i, "v": 0.123456789, "t": "x" * 50} for i in range(500)]}
    text = compact(big, 3000)
    assert len(text) <= 3000
    assert "_truncated" in text
    assert "0.1235" in text


# --------------------------------------------------------------------------- endpoint


@pytest.fixture
def client(cfg: TwinConfig, toolbox_calls: list[Any]) -> Iterator[TestClient]:
    app = create_app(cfg, clock=ManualClock(cfg.simulation.clock.demo_start), database_url=None)

    async def fake_session() -> Any:
        yield FakeSession()

    app.dependency_overrides[get_session] = fake_session
    app.state.copilot_service = service(cfg, NoneProvider())
    app.dependency_overrides[get_copilot_service] = lambda: app.state.copilot_service
    with TestClient(app) as test_client:
        yield test_client


def test_ask_endpoint_in_offline_mode(client: TestClient) -> None:
    r = client.post(
        "/api/v1/copilot/ask",
        headers=bearer("director"),
        json={"question": "Какой OEE у Окраски-1 за месяц?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "offline"
    assert body["tool_limit"] == 5
    assert not body["refused"]
    assert body["tool_calls"][0]["name"] == "get_kpi"
    assert body["basis"]["entities"] == ["PAINT-1"]
    assert body["basis"]["periods"]
    for bad in ({"question": ""}, {"question": "x", "lang": "de"}, {"question": "x", "extra": 1}):
        assert (
            client.post("/api/v1/copilot/ask", headers=bearer("director"), json=bad).status_code
            == 422
        )
    for role in ("operator", "admin"):
        denied = client.post("/api/v1/copilot/ask", headers=bearer(role), json={"question": "x"})
        assert denied.status_code == 403


# --------------------------------------------------------------------------- work-order prefill


def test_work_order_prefill_from_alerts(cfg: TwinConfig) -> None:
    now = cfg.simulation.clock.demo_start
    base = {
        "id": 3,
        "entity_type": "equipment",
        "title_ru": "t",
        "message_ru": "msg",
        "severity": "warning",
    }
    m1 = prefill_from_alert(
        cfg,
        {
            **base,
            "rule_id": "AL-M1",
            "entity": "ABB-04",
            "value": {"p_failure": 0.83, "horizon_h": 8.0},
        },
        now,
    )
    assert (m1["equipment"], m1["kind"], m1["priority"]) == ("ABB-04", "predictive", "high")
    assert "83%" in m1["title"]
    assert m1["description"] == "msg"
    assert m1["due_ts"] == cfg.calendar.next_shift_change(now)
    window = (now + timedelta(hours=8)).isoformat()
    m2 = prefill_from_alert(
        cfg,
        {
            **base,
            "rule_id": "AL-M2",
            "entity": "BOOTH-02",
            "value": {"signal_name_ru": "Перепад давления на фильтрах", "window": window},
        },
        now,
    )
    assert (m2["kind"], m2["priority"]) == ("predictive", "normal")
    assert m2["due_ts"] == now + timedelta(hours=8)
    assert "перепад давления" in m2["title"]
    s1 = prefill_from_alert(
        cfg,
        {**base, "rule_id": "AL-S1", "entity": "CONV-03", "severity": "critical", "value": {}},
        now,
    )
    assert (s1["kind"], s1["priority"], s1["due_ts"]) == ("corrective", "urgent", None)
    from qost_api.problems import ProblemError

    with pytest.raises(ProblemError):
        prefill_from_alert(
            cfg,
            {**base, "rule_id": "AL-Q1", "entity": "PAINT", "entity_type": "area", "value": 0.05},
            now,
        )
