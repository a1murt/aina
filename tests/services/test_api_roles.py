"""Role matrix (SPEC §3, §12.2, FR-UI-03, NFR-04): one declarative table → 403 expectations for
every route and every role.

* every HTTP route of the app must be in :data:`ROLE_TABLE` (a new endpoint without an entry
  fails here — add its roles from §12.2);
* the roles exported to OpenAPI (``x-roles``) equal the table;
* a role outside the table gets 403 ``/problems/forbidden`` (checked before the database, which
  is absent here); a role inside never gets 401/403; no token → 401.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from api_support import PROBLEM, bearer
from qost_api.app import create_app
from qost_api.openapi import route_roles
from twin_core.config import TwinConfig

ALL = frozenset({"director", "master", "operator", "maintenance", "quality", "admin"})
PUBLIC = None
ALERT_ACTORS = frozenset({"director", "master", "maintenance", "quality", "admin"})
"""Roles that are recipients/escalation targets of some rule in rules.yaml, plus admin."""

ROLE_TABLE: dict[tuple[str, str], frozenset[str] | None] = {
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/readyz"): PUBLIC,
    ("GET", "/api/docs"): PUBLIC,
    ("POST", "/api/v1/auth/login"): PUBLIC,
    ("GET", "/api/v1/auth/me"): ALL,
    ("GET", "/api/v1/assets"): ALL,
    ("GET", "/api/v1/config/reasons"): ALL,
    ("GET", "/api/v1/config/defects"): ALL,
    ("GET", "/api/v1/config/rules"): ALL,
    ("PATCH", "/api/v1/config/thresholds"): frozenset({"admin"}),
    ("GET", "/api/v1/live/snapshot"): ALL,
    ("GET", "/api/v1/history/timeline"): ALL,
    ("GET", "/api/v1/kpi"): ALL,
    ("GET", "/api/v1/kpi/losses"): ALL,
    ("GET", "/api/v1/plan/progress"): ALL,
    ("GET", "/api/v1/downtime"): ALL,
    ("PATCH", "/api/v1/downtime/{downtime_id}"): frozenset({"master", "operator"}),
    ("POST", "/api/v1/operator/andon"): frozenset({"operator", "master"}),
    ("POST", "/api/v1/operator/defects"): frozenset({"operator", "master"}),
    ("POST", "/api/v1/operator/material-call"): frozenset({"operator", "master"}),
    ("GET", "/api/v1/defects"): ALL,
    ("GET", "/api/v1/alerts"): ALL,
    ("POST", "/api/v1/alerts/{alert_id}/ack"): ALERT_ACTORS,
    ("POST", "/api/v1/alerts/{alert_id}/resolve"): ALERT_ACTORS,
    ("GET", "/api/v1/data-quality"): frozenset({"admin", "director", "quality"}),
    ("POST", "/api/v1/data-quality/{issue_id}/resolve"): frozenset(
        {"admin", "director", "quality"}
    ),
    ("POST", "/api/v1/import"): frozenset({"admin", "director"}),
    ("GET", "/api/v1/import/{import_id}"): frozenset({"admin", "director"}),
    ("GET", "/api/v1/import/template.xlsx"): frozenset({"admin", "director"}),
    ("POST", "/api/v1/forecast"): frozenset({"director", "admin"}),
    ("GET", "/api/v1/forecast/{run_id}"): frozenset({"director", "admin"}),
    ("GET", "/api/v1/forecast/levers"): frozenset({"director", "admin"}),
    ("GET", "/api/v1/calibration"): frozenset({"director", "admin", "maintenance"}),
    ("POST", "/api/v1/effect"): frozenset({"director", "admin"}),
    ("GET", "/api/v1/bottleneck"): ALL,
    ("GET", "/api/v1/equipment/{code}/health"): ALL,
    ("GET", "/api/v1/equipment/{code}/telemetry"): ALL,
    ("GET", "/api/v1/sim/{path}"): frozenset({"admin"}),
    ("POST", "/api/v1/sim/{path}"): frozenset({"admin"}),
}
"""SPEC §12.2, one line per route (P1 routes of later stages are added with them)."""

PATH_VALUES = {
    "{downtime_id}": "1",
    "{alert_id}": "1",
    "{issue_id}": "1",
    "{import_id}": "1",
    "{run_id}": "1",
    "{code}": "CONV-03",
    "{path}": "status",
}
POST_SIM_PATH = "pause"


def concrete(method: str, path: str) -> str:
    for key, value in PATH_VALUES.items():
        path = path.replace(key, value)
    if method == "POST" and path.endswith("/sim/status"):
        path = path.replace("/status", f"/{POST_SIM_PATH}")
    return path


@pytest.fixture(scope="module")
def client(cfg: TwinConfig) -> Iterator[TestClient]:
    with TestClient(create_app(cfg, database_url=None, redis=None)) as test_client:
        yield test_client


def test_every_route_is_in_the_table(client: TestClient) -> None:
    routes = route_roles(client.app)  # type: ignore[arg-type]
    missing = sorted(set(routes) - set(ROLE_TABLE))
    assert not missing, f"routes without a role entry (add them to ROLE_TABLE): {missing}"
    stale = sorted(set(ROLE_TABLE) - set(routes))
    assert not stale, f"table entries without a route: {stale}"


def test_openapi_roles_match_the_table(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    for (method, path), allowed in ROLE_TABLE.items():
        if path == "/api/docs":
            continue
        op = paths[path][method.lower()]
        assert set(op["x-roles"]) == set(allowed or ()), (method, path)
        if allowed:
            assert op["responses"]["403"]["content"][PROBLEM]["schema"] == {
                "$ref": "#/components/schemas/Problem"
            }
            assert {"HTTPBearer": []} in op.get("security", [])


def test_table_matches_the_alert_recipients(cfg: TwinConfig) -> None:
    recipients = {"admin"}
    for rule in cfg.rules.alert_rules:
        recipients |= set(rule.recipients)
    for level in cfg.rules.escalation.values():
        recipients |= set(level.chain)
    assert recipients == ALERT_ACTORS
    assert set(cfg.rules.roles) == ALL


CASES = [
    (method, path, role)
    for (method, path), allowed in ROLE_TABLE.items()
    if allowed is not None
    for role in sorted(ALL)
]


@pytest.mark.parametrize(("method", "path", "role"), CASES)
def test_role_matrix(client: TestClient, method: str, path: str, role: str) -> None:
    allowed = ROLE_TABLE[(method, path)]
    assert allowed is not None
    url = concrete(method, path)
    body: dict[str, object] | None = (
        {} if method in ("POST", "PATCH") and "/import" not in path else None
    )
    response = client.request(method, url, headers=bearer(role), json=body)
    if role in allowed:
        assert response.status_code not in (401, 403), (method, url, role, response.text)
    else:
        assert response.status_code == 403, (method, url, role, response.text)
        assert response.headers["content-type"] == PROBLEM
        problem: dict[str, str | int] = response.json()
        assert problem["type"] == "/problems/forbidden"
        assert problem["status"] == 403
        assert problem["instance"] == url
        assert role in str(problem["detail"])


@pytest.mark.parametrize(("method", "path"), [k for k, v in ROLE_TABLE.items() if v is not None])
def test_no_token_is_401(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, concrete(method, path))
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["type"] == "/problems/unauthorized"


def test_public_routes_need_no_token(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/api/docs").status_code == 200
