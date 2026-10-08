"""Data changes without a database (fake session): thresholds PATCH → settings + audit,
downtime PATCH → audit + operator event to ``events`` (the engine applies it), operator line
scope, RFC 7807 shapes of the refusals, the demo-console proxy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from api_support import NOW, PROBLEM, FakeRedis, FakeSession, bearer
from qost_api.app import create_app
from qost_api.db import get_session
from qost_api.routes import assets as assets_routes
from qost_api.routes import downtime as downtime_routes
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.db import AuditLog
from twin_core.events import parse_event


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


def _app(cfg: TwinConfig, session: FakeSession, redis: FakeRedis | None, **kw: Any) -> Any:
    app = create_app(
        cfg,
        clock=ManualClock(NOW),
        database_url=None,
        redis=redis,  # type: ignore[arg-type]
        **kw,
    )

    async def fake() -> AsyncIterator[FakeSession]:
        yield session

    app.dependency_overrides[get_session] = fake
    return app


@pytest.fixture
def client(cfg: TwinConfig, session: FakeSession, redis: FakeRedis) -> Iterator[TestClient]:
    with TestClient(_app(cfg, session, redis)) as test_client:
        yield test_client


def _audits(session: FakeSession) -> list[AuditLog]:
    return [o for o in session.added if isinstance(o, AuditLog)]


# --------------------------------------------------------------------------- thresholds


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {"thresholds": {"oee_target": 0.8}}

    async def fake_overrides(session: object) -> tuple[dict[str, Any], dict[str, Any]]:
        return {k: dict(v) for k, v in current.items()}, {}

    monkeypatch.setattr(assets_routes, "stored_overrides", fake_overrides)
    return current


def test_thresholds_patch_writes_settings_and_audit(
    client: TestClient, session: FakeSession, stored: dict[str, Any]
) -> None:
    response = client.patch(
        "/api/v1/config/thresholds",
        json={"thresholds": {"defect_rate_limit": 0.025, "oee_target": None}},
        headers=bearer("admin", user_id=42),
    )
    assert response.status_code == 200, response.text
    upserts = [p for stmt, p in session.executed if "INSERT INTO settings" in str(stmt)]
    assert len(upserts) == 1
    assert session.commits == 1
    [entry] = _audits(session)
    assert entry.action == "config.thresholds"
    assert entry.entity_type == "settings"
    assert entry.entity_id == "thresholds"
    assert entry.user_id == 42
    assert entry.before == {"overrides": {"oee_target": 0.8}}
    assert entry.after is not None
    assert entry.after["overrides"] == {"defect_rate_limit": 0.025}  # null removed oee_target
    assert entry.after["by"] == "admin"
    assert entry.ts == NOW


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ({"thresholds": {"oee_targ": 0.9}}, ["body", "thresholds", "oee_targ"]),
        ({"thresholds": {"defect_rate_limit": 0.05}}, ["body", "thresholds"]),  # > critical 0.04
        ({"thresholds": {"oee_target": 1.5}}, ["body", "thresholds", "oee_target"]),
        ({"data_quality": {"flow_balance_warn_units": 0}}, ["body", "data_quality"]),
    ],
)
def test_thresholds_patch_is_validated(
    client: TestClient,
    session: FakeSession,
    stored: dict[str, Any],
    body: dict[str, Any],
    where: list[str],
) -> None:
    response = client.patch("/api/v1/config/thresholds", json=body, headers=bearer("admin"))
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM
    problem = response.json()
    assert problem["type"] == "/problems/validation"
    assert any(e["loc"][: len(where)] == where for e in problem["errors"]), problem
    assert session.commits == 0
    assert not _audits(session)


def test_thresholds_patch_needs_admin(client: TestClient, stored: dict[str, Any]) -> None:
    response = client.patch(
        "/api/v1/config/thresholds",
        json={"thresholds": {"oee_target": 0.8}},
        headers=bearer("director"),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- downtime


def _stop_row(cfg: TwinConfig, line: str = "ASSY-1", **kw: Any) -> dict[str, Any]:
    start = NOW - timedelta(minutes=20)
    row = {
        "id": 5,
        "entity": "CONV-03" if line == "ASSY-1" else "ABB-01",
        "line": line,
        "start_ts": start,
        "end_ts": None,
        "duration_s": None,
        "planned": False,
        "microstop": False,
        "reason_code": "UNK",
        "reason_source": "auto",
        "shift_date": start.date(),
        "shift_code": "A",
        "comment": None,
        "classified_ts": None,
        "classified_by": None,
        "import_id": None,
        "sort_ts": start,
    }
    row.update(kw)
    return row


@pytest.fixture
def stop(monkeypatch: pytest.MonkeyPatch, cfg: TwinConfig) -> dict[str, Any]:
    holder: dict[str, Any] = {"row": _stop_row(cfg)}

    async def get_row(session: object, downtime_id: int) -> dict[str, Any] | None:
        row = holder["row"]
        return row if row is not None and row["id"] == downtime_id else None

    monkeypatch.setattr(downtime_routes, "get_downtime_row", get_row)
    return holder


def test_downtime_patch_emits_an_operator_event(
    client: TestClient, session: FakeSession, redis: FakeRedis, stop: dict[str, Any]
) -> None:
    response = client.patch(
        "/api/v1/downtime/5",
        json={"reason_code": "ME-CHAIN", "comment": "цепь"},
        headers=bearer("operator", user_id=3),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["pending"] is True
    assert body["reason_code"] == "ME-CHAIN"
    assert body["reason_source"] == "operator"
    [entry] = redis.streams["events"]
    assert entry["k"] == "operator"
    assert entry["e"] == "CONV-03"
    event = parse_event(entry["j"])
    assert event.kind == "operator"
    assert event.source == "operator"
    assert event.ts == NOW
    data = event.data.model_dump()
    assert data["action"] == "classify_downtime"
    assert data["user"] == "operator"
    assert data["payload"]["entity"] == "CONV-03"
    assert data["payload"]["reason_code"] == "ME-CHAIN"
    assert data["payload"]["comment"] == "цепь"
    assert data["payload"]["start_ts"] == stop["row"]["start_ts"].isoformat()
    assert body["event_id"] == event.event_id
    raw = [p for stmt, p in session.executed if "INSERT INTO event_raw" in str(stmt)]
    assert raw, "the event is journaled in event_raw for replay"
    [entry_audit] = _audits(session)
    assert entry_audit.action == "downtime.classify"
    assert entry_audit.entity_id == "5"
    assert entry_audit.before is not None
    assert entry_audit.before["reason_code"] == "UNK"
    assert entry_audit.after is not None
    assert entry_audit.after["reason_code"] == "ME-CHAIN"
    assert session.commits == 1


def test_operator_may_classify_only_own_line(
    client: TestClient, redis: FakeRedis, stop: dict[str, Any], cfg: TwinConfig
) -> None:
    stop["row"] = _stop_row(cfg, line="WELD-1")
    response = client.patch(
        "/api/v1/downtime/5", json={"reason_code": "RB-TOOL"}, headers=bearer("operator")
    )
    assert response.status_code == 403
    assert response.json()["type"] == "/problems/forbidden"
    assert "events" not in redis.streams
    master = client.patch(
        "/api/v1/downtime/5", json={"reason_code": "RB-TOOL"}, headers=bearer("master")
    )
    assert master.status_code == 202


def test_downtime_patch_refusals(
    client: TestClient, stop: dict[str, Any], cfg: TwinConfig, redis: FakeRedis
) -> None:
    unknown = client.patch(
        "/api/v1/downtime/5", json={"reason_code": "NOPE"}, headers=bearer("master")
    )
    assert unknown.status_code == 422
    assert unknown.json()["errors"][0]["loc"] == ["body", "reason_code"]
    missing = client.patch(
        "/api/v1/downtime/77", json={"reason_code": "ME-CHAIN"}, headers=bearer("master")
    )
    assert missing.status_code == 404
    assert missing.json()["type"] == "/problems/not-found"
    stop["row"] = _stop_row(cfg, import_id=1, start_ts=None)
    imported = client.patch(
        "/api/v1/downtime/5", json={"reason_code": "ME-CHAIN"}, headers=bearer("master")
    )
    assert imported.status_code == 409
    assert "events" not in redis.streams


def test_downtime_patch_fails_closed_without_the_stream(
    cfg: TwinConfig, session: FakeSession, redis: FakeRedis, stop: dict[str, Any]
) -> None:
    redis.fail = True
    with TestClient(_app(cfg, session, redis), raise_server_exceptions=False) as c:
        response = c.patch(
            "/api/v1/downtime/5", json={"reason_code": "ME-CHAIN"}, headers=bearer("master")
        )
    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM
    assert session.commits == 0  # nothing committed: no audit without an engine event


# --------------------------------------------------------------------------- terminal scope


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/operator/andon", {"line": "WELD-1"}),
        ("/api/v1/operator/defects", {"line": "WELD-1", "defect_code": "W-SPATTER"}),
        ("/api/v1/operator/material-call", {"line": "WELD-1"}),
    ],
)
def test_terminal_actions_are_line_scoped(
    client: TestClient, redis: FakeRedis, path: str, body: dict[str, Any]
) -> None:
    response = client.post(path, json=body, headers=bearer("operator"))
    assert response.status_code == 403, response.text
    assert "events" not in redis.streams


def test_defect_code_must_belong_to_the_area(client: TestClient, cfg: TwinConfig) -> None:
    paint_only = next(d.code for d in cfg.defect_codes.defects if d.area == "PAINT")
    response = client.post(
        "/api/v1/operator/defects",
        json={"line": "ASSY-1", "defect_code": paint_only},
        headers=bearer("operator"),
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "defect_code"]


# --------------------------------------------------------------------------- RFC 7807 + docs


def test_problem_shapes(client: TestClient) -> None:
    for response, status, slug in (
        (client.get("/api/v1/nope", headers=bearer("admin")), 404, None),
        (client.get("/api/v1/kpi?level=bogus", headers=bearer("admin")), 422, "validation"),
        (client.get("/api/v1/equipment/NOPE/telemetry", headers=bearer("admin")), 404, "not-found"),
        (client.get("/api/v1/assets"), 401, "unauthorized"),
    ):
        assert response.status_code == status
        assert response.headers["content-type"] == PROBLEM
        body = response.json()
        assert body["status"] == status
        assert body["title"]
        assert body["instance"].startswith("/api/v1/")
        if slug:
            assert body["type"] == f"/problems/{slug}"


def test_docs_are_offline(client: TestClient) -> None:
    html = client.get("/api/docs").text
    assert "cdn" not in html.lower()
    assert "fastapi.tiangolo.com" not in html
    assert '"validatorUrl": null' in html
    assert client.get("/api/docs/assets/swagger-ui-bundle.js").status_code == 200
    schema = client.get("/api/openapi.json").json()
    assert "Problem" in schema["components"]["schemas"]
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_assets_carry_the_schema_layout(client: TestClient, cfg: TwinConfig) -> None:
    body = client.get("/api/v1/assets", headers=bearer("operator")).json()
    assert body["site"]["timezone"] == "Asia/Qostanay"
    assert body["flow"] == list(cfg.flow_lines)
    assert body["layout"]["flow_path"][0] == [140.0, 280.0]
    assert {p["code"]: p["color_hex"] for p in body["products"]}["ONIX"] == "#5B8DEF"
    conv = next(
        e
        for a in body["areas"]
        for line in a["lines"]
        for e in line["equipment"]
        if e["code"] == "CONV-03"
    )
    assert conv["layout"] == {"x": 1175.0, "y": 345.0}
    assert conv["criticality"] == "A"
    booth = body["equipment_types"]["booth"]["signals"][0]
    assert booth["code"] == "filter_dp_pa"
    assert booth["limit_hi"] == 450
    assert body["buffers"][0]["layout"]["w"] == 60


def test_config_endpoints(client: TestClient, cfg: TwinConfig) -> None:
    reasons = client.get("/api/v1/config/reasons", headers=bearer("operator")).json()
    assert reasons["categories"][0]["reasons"][0]["code"] == "PM-SCHEDULED"
    defects = client.get("/api/v1/config/defects", headers=bearer("operator")).json()
    assert len(defects["defects"]) == len(cfg.defect_codes.defects)
    rules = client.get("/api/v1/config/rules", headers=bearer("quality")).json()
    assert rules["thresholds"]["oee_target"] == 0.85  # no database: YAML only
    assert {r["id"] for r in rules["alert_rules"]} >= {"AL-S1", "AL-A1", "AL-P1"}


# --------------------------------------------------------------------------- demo console proxy


def test_sim_proxy(cfg: TwinConfig, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path == "/inject":
            return httpx2.Response(404, json={"detail": "unknown scenario 'S9'"})
        return httpx2.Response(200, json={"state": "running", "speed": 60})

    monkeypatch.setenv("SIM_CONTROL_URL", "http://sim:8100")
    app = _app(cfg, session, None, sim_transport=httpx2.MockTransport(handler))
    with TestClient(app) as c:
        assert c.get("/api/v1/sim/status", headers=bearer("admin")).json()["speed"] == 60
        ok = c.post("/api/v1/sim/speed", json={"value": 300}, headers=bearer("admin"))
        assert ok.status_code == 200
        bad = c.post("/api/v1/sim/inject", json={"scenario_id": "S9"}, headers=bearer("admin"))
        assert bad.status_code == 404
        assert bad.headers["content-type"] == PROBLEM
        assert bad.json()["detail"] == "unknown scenario 'S9'"
        assert c.post("/api/v1/sim/shutdown", headers=bearer("admin")).status_code == 404
    assert ("POST", "/speed", json.dumps({"value": 300}).encode()) in [
        (m, p, json.dumps(json.loads(b)).encode() if b else b) for m, p, b in seen
    ]


def test_sim_proxy_without_console(client: TestClient) -> None:
    response = client.get("/api/v1/sim/status", headers=bearer("admin"))
    assert response.status_code == 503
    assert response.json()["type"] == "/problems/sim-unavailable"
