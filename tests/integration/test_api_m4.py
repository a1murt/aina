"""M4 API against TimescaleDB + Redis: seed (FR-DB-01), history (backfill + replay), case import
through the API, KPI / losses / timeline / plan / bottleneck / equipment over that history,
director queries p95 ≤ 300 ms over a full month (FR-DB-02), alerts and DQ flows, operator
actions → ``events`` stream, thresholds in ``settings``, AL-P1.

Isolation: database ``<TEST_DATABASE_URL db>_it_m4`` (dropped and re-created), Redis DB 14 with
prefixed keys, streams and channel — the shared stack is not touched.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import statistics
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from m3_stack import REDIS_BASE, create_database, drop_database, query, url_string
from redis.asyncio import Redis
from sqlalchemy.engine import URL

from api_support import PROBLEM
from qost_api.app import create_app
from qost_api.db import make_engine, make_sessionmaker
from qost_api.plan_risk import record_plan_risk
from qost_api.seed import run_seed
from qost_api.settings import ApiSettings
from qost_engine.replay import run_replay
from qost_sim.backfill import run_backfill
from support import CASE_DOCX, REPO_ROOT, golden, require_case_docx
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.db.sink import DbSink

pytestmark = pytest.mark.integration

REDIS_URL = REDIS_BASE.rsplit("/", 1)[0] + "/14"
PREFIX = f"itm4{uuid.uuid4().hex[:6]}:"
ENV = {
    "REDIS_URL": REDIS_URL,
    "LIVE_PREFIX": f"{PREFIX}live:",
    "LIVE_CHANNEL": f"{PREFIX}live",
    "EVENTS_STREAM": f"{PREFIX}events",
    "ALERTS_STREAM": f"{PREFIX}alerts",
    "CLOCK_MODE": "sim",
}
P95_LIMIT_S = 0.300


def _redis_call(fn: Any) -> Any:
    async def run() -> Any:
        redis = Redis.from_url(REDIS_URL)
        try:
            return await fn(redis)
        finally:
            await redis.aclose()

    return asyncio.run(run())


async def _seed(url: URL, cfg: TwinConfig) -> dict[str, Any]:
    engine = make_engine(url_string(url))
    try:
        report = await run_seed(
            make_sessionmaker(engine),
            cfg,
            ApiSettings(),
            around=cfg.simulation.clock.demo_start.astimezone(cfg.timezone).date(),
            now=cfg.simulation.clock.demo_start,
        )
        return report.as_dict()
    finally:
        await engine.dispose()


async def _history(url: URL, cfg: TwinConfig) -> dict[str, float]:
    started = time.perf_counter()
    sink = DbSink(
        url_string(url), area_of_line={line: cfg.area_of_line(line).code for line in cfg.lines}
    )
    try:
        await run_backfill(cfg, sink, batch_size=5000)
    finally:
        await sink.aclose()
    backfill_s = time.perf_counter() - started
    await run_replay(cfg, database_url=url_string(url))
    return {"backfill_s": backfill_s, "total_s": time.perf_counter() - started}


@pytest.fixture(scope="module")
def env() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        for key, value in ENV.items():
            mp.setenv(key, value)
        yield
    _redis_call(lambda r: _drop_prefixed(r))


async def _drop_prefixed(redis: Redis) -> None:
    keys = [k async for k in redis.scan_iter(match=f"{PREFIX}*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture(scope="module")
def db(cfg: TwinConfig, env: None) -> Iterator[URL]:
    url = create_database("it_m4")
    first = asyncio.run(_seed(url, cfg))
    assert first["users_created"] == list(cfg.rules.roles)
    timing = asyncio.run(_history(url, cfg))
    Path(REPO_ROOT / "var").mkdir(exist_ok=True)
    (REPO_ROOT / "var" / "m4_history.json").write_text(json.dumps(timing))
    yield url
    drop_database(url)


@pytest.fixture(scope="module")
def client(db: URL, cfg: TwinConfig) -> Iterator[TestClient]:
    app = create_app(
        cfg, clock=ManualClock(cfg.simulation.clock.demo_start), database_url=url_string(db)
    )
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, user: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": user, "password": ApiSettings().demo_password.get_secret_value()},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="module")
def imported(client: TestClient) -> dict[str, Any]:
    require_case_docx()
    files = {"files": (CASE_DOCX.name, CASE_DOCX.read_bytes())}
    response = client.post("/api/v1/import", files=files, headers=login(client, "director"))
    assert response.status_code == 201, response.text
    again = client.post("/api/v1/import", files=files, headers=login(client, "admin"))
    assert again.status_code == 200  # idempotent (FR-IMP-06)
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------- seed


def test_seed_is_idempotent(db: URL, cfg: TwinConfig) -> None:
    before = asyncio.run(query(db, "SELECT id, username, role FROM app_user ORDER BY id"))
    again = asyncio.run(_seed(db, cfg))
    assert again["users_created"] == []
    assert again["users_updated"] == []
    assert again["plan_rows_added"] == 0
    after = asyncio.run(query(db, "SELECT id, username, role FROM app_user ORDER BY id"))
    assert after == before
    assert {r["username"]: r["role"] for r in after} == {r: r for r in cfg.rules.roles}
    counts = asyncio.run(
        query(
            db,
            "SELECT (SELECT count(*) FROM shift) AS shifts, "
            "(SELECT count(*) FROM shift WHERE working) AS working, "
            "(SELECT count(*) FROM asset_equipment) AS equipment, "
            "(SELECT count(*) FROM reason_code) AS reasons, "
            "(SELECT count(*) FROM production_plan WHERE month = '2026-10') AS plan, "
            "(SELECT value FROM settings WHERE key = 'operator_lines') AS lines",
        )
    )[0]
    assert counts["shifts"] == 241 * len(cfg.plant.calendar.shifts)
    assert 0 < counts["working"] < counts["shifts"]
    assert counts["equipment"] == len(cfg.equipment)
    assert counts["reasons"] == len(cfg.reasons)
    assert counts["plan"] == 4
    assert counts["lines"] == {"operator": ["ASSY-1"]}


def test_login_and_profile(client: TestClient) -> None:
    me = client.get("/api/v1/auth/me", headers=login(client, "operator")).json()
    assert me["role"] == "operator"
    assert me["lines"] == ["ASSY-1"]
    assert client.get("/readyz").json()["checks"]["database"] == "ok"


# --------------------------------------------------------------------------- KPI over history


def test_kpi_import_wins_over_events(client: TestClient, imported: dict[str, Any]) -> None:
    headers = login(client, "director")
    rows = client.get(
        "/api/v1/kpi?level=line&from=2026-10-01&to=2026-10-02&granularity=shift",
        headers=headers,
    ).json()["items"]
    by_key = {(r["code"], r["date"], r["shift"]): r for r in rows}
    for report in golden()["shift_reports"]:
        row = by_key[(report["line"], report["date"], "A")]
        assert row["source"] == "import"
        assert row["oee"] == pytest.approx(report["oee"], abs=1e-4)
        assert row["availability"] == pytest.approx(report["availability"], abs=1e-4)
    # shift B of those days and other lines come from the events
    assert by_key[("WELD-1", "2026-10-01", "B")]["source"] == "events"
    assert by_key[("QC-1", "2026-10-01", "A")]["source"] == "events"


def test_kpi_levels(client: TestClient, cfg: TwinConfig) -> None:
    headers = login(client, "master")
    days = client.get(
        "/api/v1/kpi?level=line&code=PAINT-1&from=2026-10-05&to=2026-10-09", headers=headers
    ).json()["items"]
    assert [d["date"] for d in days] == [f"2026-10-0{i}" for i in range(5, 10)]
    for d in days:
        assert d["pot"] == 960
        assert d["pbt"] == pytest.approx(d["pot"] - d["pdot"])
        # OEE = Σ PRI(GQ) / PBT; A × E × QR equals it up to the product mix (cycle factors)
        product = d["availability"] * d["effectiveness"] * d["quality_ratio"]
        assert d["oee"] == pytest.approx(product, rel=5e-3)
    month = client.get(
        "/api/v1/kpi?level=plant&from=2026-09-01&to=2026-09-30&granularity=month", headers=headers
    ).json()["items"]
    assert len(month) == 1
    assert month[0]["source"] == "events"
    assert 0 < month[0]["rty"] < 1
    areas = client.get(
        "/api/v1/kpi?level=area&from=2026-09-01&to=2026-09-30&granularity=month", headers=headers
    ).json()["items"]
    assert {a["code"] for a in areas} == {"WELD", "PAINT", "ASSY", "QC"}
    eq = client.get(
        "/api/v1/kpi?level=equipment&code=CONV-03&from=2026-09-01&to=2026-09-30&granularity=month",
        headers=headers,
    ).json()["items"]
    assert eq[0]["failures"] >= 0
    assert 0 < eq[0]["availability"] <= 1


def test_losses(client: TestClient, cfg: TwinConfig) -> None:
    body = client.get(
        "/api/v1/kpi/losses?from=2026-09-01&to=2026-09-30", headers=login(client, "director")
    ).json()
    cats = {c["category"]: c for c in body["categories"]}
    assert set(cats) == {
        "planned_downtime", "unplanned_downtime", "starved", "blocked", "changeover",
        "microstops", "speed", "quality",
    }  # fmt: skip
    assert body["totals"]["units"] == pytest.approx(
        sum(c["units"] for c in body["categories"] if c["loss"]), abs=0.05
    )
    assert body["kzt_per_car"] == 700000
    assert body["totals"]["kzt"] == pytest.approx(body["totals"]["units"] * 700000, rel=1e-3)
    unplanned = [i for i in body["items"] if i["category"] == "unplanned_downtime"]
    assert any(i["equipment"] for i in unplanned), "stops are attributed to units"
    assert body["items"][0]["loss"] is True
    units = [i["units"] for i in body["items"] if i["loss"]]
    assert units == sorted(units, reverse=True)


def test_timeline(client: TestClient) -> None:
    body = client.get(
        "/api/v1/history/timeline?entity=ASSY-1,CONV-03&from=2026-10-15T07:00:00%2B05:00"
        "&to=2026-10-15T15:00:00%2B05:00",
        headers=login(client, "operator"),
    ).json()
    line = next(e for e in body["entities"] if e["code"] == "ASSY-1")
    spans = line["intervals"]
    assert spans
    for a, b in itertools.pairwise(spans):
        assert a["end"] == b["start"]  # contiguous


def test_plan_progress_matches_events(client: TestClient, db: URL, cfg: TwinConfig) -> None:
    body = client.get(
        "/api/v1/plan/progress?month=2026-10", headers=login(client, "quality")
    ).json()
    rows = asyncio.run(
        query(
            db,
            "SELECT count(*) AS n FROM unit_event WHERE line = 'QC-1' "
            "AND result IN ('pass', 'rework_pass') AND ts >= '2026-09-30T19:00:00Z' "
            "AND ts < '2026-10-16T02:00:00Z'",
        )
    )
    assert body["mtd_output"] == rows[0]["n"] > 0
    assert body["shifts"] == {"total": 42, "elapsed": 22.0, "remaining": 20.0}
    line = body["targets"]["line_plan"]
    assert line["qty"] == 4800
    assert line["required_rate"] == pytest.approx((4800 - body["mtd_output"]) / 20, abs=1e-4)
    assert body["targets"]["plant_target"]["qty"] == 5500
    assert body["unallocated"] == -700
    assert body["daily"][14]["cum_output"] == body["mtd_output"]  # through 15.10
    start = client.get("/api/v1/plan/progress?month=2026-11", headers=login(client, "quality"))
    assert start.json()["mtd_output"] == 0


def test_bottleneck(client: TestClient, imported: dict[str, Any]) -> None:
    body = client.get(
        "/api/v1/bottleneck?from=2026-09-01&to=2026-10-15", headers=login(client, "master")
    ).json()
    assert body["shifts"] > 40
    assert body["overall"] in ("PAINT-1", "WELD-1")
    for shares in body["shares"].values():
        assert 0 <= shares["sole"] + shares["shifting"] <= 1
    aggregate = body["aggregate"]
    assert aggregate["line"] == "WELD-1"  # golden: 01.10 PAINT, 02.10 WELD, period WELD
    assert aggregate["shifting"] is True
    assert [d["line"] for d in aggregate["days"]] == ["PAINT-1", "WELD-1"]


def test_equipment_health_and_telemetry(client: TestClient) -> None:
    headers = login(client, "maintenance")
    health = client.get("/api/v1/equipment/BOOTH-02/health", headers=headers).json()
    dp = next(s for s in health["signals"] if s["code"] == "filter_dp_pa")
    assert dp["value"] is not None
    assert dp["status"] in ("normal", "warning", "limit")
    assert health["prediction"] is None  # PdM serving arrives with M7b
    assert health["reliability_30d"]["pot_min"] > 0
    tele = client.get(
        "/api/v1/equipment/BOOTH-02/telemetry?signal=filter_dp_pa&from=2026-10-14&to=2026-10-15",
        headers=headers,
    ).json()
    assert tele["agg"] == "15m"
    points = tele["signals"][0]["points"]
    assert len(points) > 50
    assert all(p[2] <= p[1] <= p[3] for p in points)


# --------------------------------------------------------------------------- FR-DB-02


def test_director_queries_p95(client: TestClient) -> None:
    headers = login(client, "director")
    month = "from=2026-09-01&to=2026-09-30"
    urls = [
        f"/api/v1/kpi?level=plant&{month}&granularity=day",
        f"/api/v1/kpi?level=area&{month}&granularity=month",
        f"/api/v1/kpi?level=line&{month}&granularity=shift",
        f"/api/v1/kpi/losses?{month}",
        "/api/v1/plan/progress?month=2026-09",
        f"/api/v1/bottleneck?{month}",
        "/api/v1/alerts?limit=50",
    ]
    samples: dict[str, list[float]] = {u: [] for u in urls}
    for url in urls:
        assert client.get(url, headers=headers).status_code == 200  # warm-up
    for _ in range(20):
        for url in urls:
            started = time.perf_counter()
            response = client.get(url, headers=headers)
            samples[url].append(time.perf_counter() - started)
            assert response.status_code == 200
    every = sorted(x for xs in samples.values() for x in xs)
    p95 = every[int(0.95 * (len(every) - 1))]
    report = {
        "p95_s": p95,
        "per_url_p95_s": {u: sorted(xs)[int(0.95 * (len(xs) - 1))] for u, xs in samples.items()},
        "median_s": statistics.median(every),
    }
    (REPO_ROOT / "var" / "m4_fr_db_02.json").write_text(json.dumps(report, indent=2))
    assert p95 <= P95_LIMIT_S, report


# --------------------------------------------------------------------------- actions


def test_alert_ack_flow(client: TestClient, db: URL, imported: dict[str, Any]) -> None:
    items = client.get(
        "/api/v1/alerts?status=open&rule_id=AL-Q1&limit=10", headers=login(client, "operator")
    ).json()["items"]
    alert = next(a for a in items if a["severity"] == "critical")
    url = f"/api/v1/alerts/{alert['id']}"
    assert alert["can_act"] is False
    assert client.post(f"{url}/ack", headers=login(client, "operator")).status_code == 403
    denied = client.post(f"{url}/ack", headers=login(client, "maintenance"))
    assert denied.status_code == 403  # not a recipient of AL-Q1
    assert denied.headers["content-type"] == PROBLEM
    ack = client.post(f"{url}/ack", json={"comment": "смотрим"}, headers=login(client, "quality"))
    assert ack.status_code == 200, ack.text
    assert ack.json()["status"] == "ack"
    resolved = client.post(f"{url}/resolve", headers=login(client, "master"))
    assert resolved.json()["status"] == "resolved"
    assert client.post(f"{url}/ack", headers=login(client, "admin")).status_code == 409
    audit = asyncio.run(
        query(
            db,
            f"SELECT action FROM audit_log WHERE entity_type='alert' AND entity_id='{alert['id']}'",
        )
    )
    assert sorted(r["action"] for r in audit) == ["alert.ack", "alert.resolve"]
    row = asyncio.run(query(db, f"SELECT status, ack_by FROM alert WHERE id = {alert['id']}"))[0]
    assert row["status"] == "resolved"
    assert row["ack_by"] is not None
    stream = _redis_call(lambda r: r.xrange(ENV["ALERTS_STREAM"]))
    assert any(json.loads(f[b"j"])["id"] == alert["id"] for _, f in stream)
    counts = _redis_call(lambda r: r.get(f"{ENV['LIVE_PREFIX']}alerts_open"))
    assert json.loads(counts)["critical"] >= 0


def test_data_quality_review(client: TestClient, imported: dict[str, Any]) -> None:
    headers = login(client, "quality")
    job_id = imported["job"]["id"]
    items = client.get(f"/api/v1/data-quality?import_id={job_id}", headers=headers).json()["items"]
    assert len(items) == 10  # golden: DQ-01x1, DQ-02x5, DQ-03x1, DQ-04x2, DQ-05x1
    first = items[0]
    done = client.post(
        f"/api/v1/data-quality/{first['id']}/resolve", json={"comment": "ok"}, headers=headers
    )
    assert done.json()["status"] == "resolved"
    assert client.get("/api/v1/data-quality", headers=login(client, "master")).status_code == 403


def test_downtime_classification_goes_to_the_engine(client: TestClient, db: URL) -> None:
    listing = client.get(
        "/api/v1/downtime?line=ASSY-1&entity_type=equipment&limit=5",
        headers=login(client, "operator"),
    ).json()
    assert listing["next_cursor"]
    stop = listing["items"][0]
    page2 = client.get(
        f"/api/v1/downtime?line=ASSY-1&entity_type=equipment&limit=5&cursor={listing['next_cursor']}",
        headers=login(client, "operator"),
    ).json()
    assert page2["items"][0]["id"] not in {i["id"] for i in listing["items"]}
    response = client.patch(
        f"/api/v1/downtime/{stop['id']}",
        json={"reason_code": "ME-BEARING", "comment": "подшипник"},
        headers=login(client, "operator"),
    )
    assert response.status_code == 202, response.text
    event_id = response.json()["event_id"]
    raw = asyncio.run(
        query(db, f"SELECT kind, source, data FROM event_raw WHERE event_id = '{event_id}'")
    )
    assert raw[0]["kind"] == "operator"
    assert raw[0]["data"]["payload"]["entity"] == stop["entity"]
    stream = _redis_call(lambda r: r.xrange(ENV["EVENTS_STREAM"]))
    assert any(json.loads(f[b"j"])["event_id"] == event_id for _, f in stream)
    listed = client.get(
        f"/api/v1/downtime?entity={stop['entity']}&limit=50", headers=login(client, "master")
    ).json()["items"]
    assert next(i for i in listed if i["id"] == stop["id"])["classified_by"] == "operator"


def test_operator_actions(client: TestClient, db: URL) -> None:
    headers = login(client, "operator")
    first = client.post(
        "/api/v1/operator/andon",
        json={"line": "ASSY-1", "reason_code": "ME-CHAIN"},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/operator/andon", json={"line": "ASSY-1"}, headers=headers)
    assert second.json()["alert"]["id"] == first.json()["alert"]["id"]
    assert second.json()["alert"]["value"]["count"] == 2
    assert "андон" in second.json()["alert"]["message_ru"]
    assert (
        client.post("/api/v1/operator/andon", json={"line": "WELD-1"}, headers=headers).status_code
        == 403
    )
    defect = client.post(
        "/api/v1/operator/defects",
        json={"line": "ASSY-1", "defect_code": "A-TORQUE", "qty": 2, "body_id": "B123"},
        headers=headers,
    )
    assert defect.status_code == 201, defect.text
    listed = client.get("/api/v1/defects?source=operator", headers=login(client, "quality")).json()
    assert listed["items"][0]["id"] == defect.json()["id"]
    assert listed["items"][0]["created_by"] == "operator"
    call = client.post(
        "/api/v1/operator/material-call", json={"line": "ASSY-1", "product": "J7"}, headers=headers
    )
    assert call.json()["alert"]["rule_id"] == "AL-A2"
    actions = asyncio.run(
        query(db, "SELECT data->>'action' AS a FROM event_raw WHERE kind = 'operator'")
    )
    assert {"andon", "log_defect", "material_call"} <= {r["a"] for r in actions}
    ack = client.post(
        f"/api/v1/alerts/{first.json()['alert']['id']}/ack", headers=login(client, "master")
    )
    assert ack.json()["status"] == "ack"


def test_thresholds_in_settings(client: TestClient, db: URL) -> None:
    admin = login(client, "admin")
    changed = client.patch(
        "/api/v1/config/thresholds", json={"thresholds": {"oee_target": 0.8}}, headers=admin
    )
    assert changed.status_code == 200, changed.text
    rules = client.get("/api/v1/config/rules", headers=login(client, "master")).json()
    assert rules["thresholds"]["oee_target"] == 0.8
    assert rules["defaults"]["thresholds"]["oee_target"] == 0.85
    assert rules["overrides_meta"]["thresholds"]["updated_by"] is not None
    client.patch(
        "/api/v1/config/thresholds", json={"thresholds": {"oee_target": None}}, headers=admin
    )
    assert (
        client.get("/api/v1/config/rules", headers=admin).json()["thresholds"]["oee_target"] == 0.85
    )
    audit = asyncio.run(
        query(db, "SELECT count(*) AS n FROM audit_log WHERE action = 'config.thresholds'")
    )
    assert audit[0]["n"] == 2


def test_plan_risk_alert(client: TestClient, db: URL, cfg: TwinConfig) -> None:
    app: Any = client.app
    portal = client.portal
    assert portal is not None

    def risk(overrides: dict[str, Any], result: dict[str, Any]) -> str | None:
        outcome: str | None = portal.call(record_plan_risk, app, "2026-10", overrides, result)
        return outcome

    low = {"p_reach": {"line_plan": 0.1}, "targets": {"line_plan": 4800}, "summary": {"p50": 4500}}
    assert risk({}, low) == "opened"
    rows = asyncio.run(
        query(db, "SELECT severity, status, message_ru FROM alert WHERE rule_id = 'AL-P1'")
    )
    assert rows, "AL-P1 opened for a low P(plan)"
    assert rows[0]["severity"] == "critical"
    assert "4 800" in rows[0]["message_ru"] or "4800" in rows[0]["message_ru"]
    high = {"p_reach": {"line_plan": 0.9}, "targets": {"line_plan": 4800}}
    assert risk({}, high) == "resolved"
    assert risk({"defect_rate": {"PAINT": 0.1}}, low) is None  # what-if scenarios never raise it


def test_open_downtime_window(client: TestClient) -> None:
    body = client.get(
        "/api/v1/downtime?from=2026-10-15&to=2026-10-15&microstop=true&limit=500",
        headers=login(client, "director"),
    ).json()
    assert body["items"]
    assert all(i["microstop"] for i in body["items"])
    assert timedelta(0) == timedelta(0)
    assert os.environ["LIVE_PREFIX"].startswith(PREFIX)


def test_ws_over_redis_pubsub(client: TestClient) -> None:
    """Real Redis: the hub subscribes to the (prefixed) live channel and forwards deltas."""
    tok = login(client, "master")["Authorization"].split()[1]
    _redis_call(
        lambda r: r.hset(
            f"{ENV['LIVE_PREFIX']}equipment",
            "CONV-03",
            json.dumps({"code": "CONV-03", "state": "DOWN_UNPLANNED", "alarm": True}),
        )
    )
    with client.websocket_connect(f"/ws/live?token={tok}") as ws:
        snap = json.loads(ws.receive_text())
        assert snap["type"] == "snapshot"
        conv = next(e for e in snap["data"]["equipment"] if e["code"] == "CONV-03")
        assert conv["state"] == "DOWN_UNPLANNED"
        envelope = json.dumps(
            {"type": "state", "ts": "2026-10-16T02:00:01Z", "data": {"code": "CONV-03"}}
        )
        deadline = time.monotonic() + 5
        got: dict[str, Any] = {}
        while time.monotonic() < deadline and got.get("type") != "state":
            _redis_call(lambda r: r.publish(ENV["LIVE_CHANNEL"], envelope))
            got = json.loads(ws.receive_text())
        assert got["type"] == "state"
        assert got["data"]["code"] == "CONV-03"
