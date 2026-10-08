"""Import API against TimescaleDB (FR-IMP-04/05/06): migrations, POST/GET, persistence, idempotency.

Uses its own database ``<TEST_DATABASE_URL db>_it_import`` on the same server (dropped and
re-created for the module, migrated to head), so it never touches data of a running stack.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from api_support import bearer
from qost_api.app import create_app
from support import (
    CASE_CSVS,
    CASE_DOCX,
    CASE_XLSX,
    REPO_ROOT,
    golden_differences,
    require_case_docx,
)
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig

pytestmark = pytest.mark.integration

BASE_URL = make_url(
    os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://qost:qost@localhost:5432/qost")
)
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
DIRECTOR = bearer("director", username="it-director")
DATA_TABLES = (
    "audit_log",
    "alert",
    "dq_issue",
    "downtime",
    "kpi_shift",
    "shift_report",
    "production_plan",
    "import_job",
)


async def _admin(sql: str) -> None:
    engine = create_async_engine(BASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def query(url: URL, sql: str) -> list[dict[str, Any]]:
    """Run one statement in its own engine and event loop; rows as dicts."""

    async def run() -> list[dict[str, Any]]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [dict(row) for row in result.mappings()] if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def count(url: URL, table: str, where: str = "TRUE") -> int:
    return int(query(url, f"SELECT count(*) AS n FROM {table} WHERE {where}")[0]["n"])


@pytest.fixture(scope="module")
def db_url() -> Iterator[URL]:
    name = f"{BASE_URL.database}_it_import"
    url = BASE_URL.set(database=name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        command.upgrade(Config(str(REPO_ROOT / "services/api/alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    yield url
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def client(db_url: URL, cfg: TwinConfig) -> Iterator[TestClient]:
    tables = ", ".join(DATA_TABLES)
    query(db_url, f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    app = create_app(
        cfg, clock=ManualClock(NOW), database_url=db_url.render_as_string(hide_password=False)
    )
    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, *paths: Any, headers: dict[str, str] | None = None) -> Any:
    files = [("files", (p.name, p.read_bytes())) for p in paths]
    return client.post("/api/v1/import", files=files, headers=headers or DIRECTOR)


def test_migrations_create_the_schema(db_url: URL) -> None:
    info = "timescaledb_information"
    hypertables = {
        r["hypertable_name"]
        for r in query(db_url, f"SELECT hypertable_name FROM {info}.hypertables")
    }
    assert hypertables == {
        "event_raw",
        "telemetry",
        "equipment_state",
        "unit_event",
        "buffer_level",
        "ckd_stock",
        "prediction",
    }
    views = {
        r["view_name"] for r in query(db_url, f"SELECT view_name FROM {info}.continuous_aggregates")
    }
    assert views == {"telemetry_15m", "telemetry_1h"}
    head = ScriptDirectory.from_config(Config(str(REPO_ROOT / "services/api/alembic.ini")))
    assert query(db_url, "SELECT version_num FROM alembic_version") == [
        {"version_num": head.get_current_head()}
    ]


def test_docx_import_round_trip_and_idempotency(client: TestClient, db_url: URL) -> None:
    require_case_docx()
    created = post(client, CASE_DOCX)
    assert created.status_code == 201, created.text
    body = created.json()
    assert golden_differences(body) == []
    job = body["job"]
    assert job["created"] is True
    assert job["kind"] == "docx"
    assert created.headers["location"] == f"/api/v1/import/{job['id']}"

    fetched = client.get(f"/api/v1/import/{job['id']}", headers=DIRECTOR)
    assert fetched.status_code == 200
    assert golden_differences(fetched.json()) == []
    assert fetched.json()["job"]["sha256"] == job["sha256"]

    expected_counts = {
        "import_job": 1,
        "shift_report": 6,
        "kpi_shift": 6,
        "downtime": 4,
        "production_plan": 4,
        "dq_issue": 10,
        "alert": 6,
        "audit_log": 1,
    }
    assert {t: count(db_url, t) for t in expected_counts} == expected_counts

    again = post(client, CASE_DOCX)
    assert again.status_code == 200
    assert again.json()["job"] == {**job, "created": False}
    assert {t: count(db_url, t) for t in expected_counts} == expected_counts

    # What was persisted (FR-IMP-04).
    assert count(db_url, "shift_report", "shift_code = 'A' AND source = 'import'") == 6
    assert count(db_url, "kpi_shift", "source = 'import' AND version = 1 AND final") == 6
    oee = query(
        db_url,
        "SELECT oee, availability FROM kpi_shift "
        "WHERE line = 'WELD-1' AND shift_date = '2026-10-02'",
    )[0]
    assert round(oee["oee"], 4) == 0.8738
    assert oee["availability"] == 0.9
    assert (
        count(
            db_url,
            "downtime",
            "reason_source = 'import' AND shift_code IS NULL AND start_ts IS NULL "
            "AND NOT microstop",
        )
        == 4
    )
    chain = query(db_url, "SELECT duration_s, planned, line FROM downtime WHERE entity = 'CONV-03'")
    assert chain == [{"duration_s": 3300.0, "planned": False, "line": "ASSY-1"}]
    plan = {
        (r["level"], r["product"]): (r["line"], r["qty"])
        for r in query(db_url, "SELECT level, line, product, qty FROM production_plan")
    }
    assert plan == {
        ("line_model", "ONIX"): ("ASSY-1", 2500),
        ("line_model", "COBALT"): ("ASSY-1", 1800),
        ("line_model", "J7"): ("ASSY-1", 500),
        ("plant_target", None): (None, 5500),
    }
    alerts = {
        r["dedup_key"]: (r["severity"], r["value"], r["ts"])
        for r in query(db_url, "SELECT dedup_key, severity, value::text, ts FROM alert")
    }
    assert alerts["AL-Q1|PAINT|2026-10-02/A"][:2] == ("critical", "0.0517")
    assert alerts["AL-D1|CONV-03|2026-10-02"][:2] == ("warning", "55.0")
    assert {v[2] for v in alerts.values()} == {NOW}
    audit = query(db_url, "SELECT action, entity_type, entity_id, after FROM audit_log")[0]
    assert (audit["action"], audit["entity_type"], audit["entity_id"]) == (
        "import.create",
        "import_job",
        str(job["id"]),
    )
    assert audit["after"]["by"] == "it-director"
    assert audit["after"]["rows"]["shift_report"] == 6


def test_xlsx_and_csv_reimport_the_same_period(client: TestClient, db_url: URL) -> None:
    xlsx = post(client, CASE_XLSX)
    assert xlsx.status_code == 201, xlsx.text
    assert golden_differences(xlsx.json()) == []
    csv = post(client, *CASE_CSVS)
    assert csv.status_code == 201, csv.text
    assert golden_differences(csv.json()) == []
    assert csv.json()["job"]["kind"] == "csv"
    assert csv.json()["job"]["filename"] == ", ".join(p.name for p in CASE_CSVS)

    assert count(db_url, "import_job") == 2
    assert count(db_url, "shift_report") == 6, "upsert per line and shift"
    assert count(db_url, "kpi_shift") == 12, "a new KPI version per import"
    assert count(db_url, "kpi_shift", "version = 2") == 6
    assert count(db_url, "downtime") == 4, "the latest import replaces imported entries"
    assert count(db_url, "alert") == 6, "deduplicated by rule + entity + period"
    assert count(db_url, "dq_issue") == 20
    assert count(db_url, "production_plan") == 3, "no plant target without text constraints"
    assert count(db_url, "audit_log") == 2

    reordered = post(client, *reversed(CASE_CSVS))
    assert reordered.status_code == 200, "same content in another order is the same upload"


def test_bad_upload_is_a_problem_and_writes_nothing(client: TestClient, db_url: URL) -> None:
    response = client.post(
        "/api/v1/import", files=[("files", ("broken.docx", b"not a docx"))], headers=DIRECTOR
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "/problems/import-format"
    assert count(db_url, "import_job") == 0
    missing = client.post("/api/v1/import", headers=DIRECTOR)
    assert missing.status_code == 422
    assert missing.json()["type"] == "/problems/validation"


def test_roles_and_missing_imports(client: TestClient) -> None:
    require_case_docx()
    assert post(client, CASE_DOCX, headers=bearer("operator")).status_code == 403
    response = client.get("/api/v1/import/999", headers=bearer("admin"))
    assert response.status_code == 404
    assert response.json()["type"] == "/problems/not-found"
