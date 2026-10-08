"""M7b on real processes: history (backfill + replay) → live sim at 300× with S2 and S3 at
``demo_start`` → the engine's PdM serving writes ``prediction`` rows, health in ``live:equipment``,
AL-M1 for ABB-04 (S3) and AL-M2 for BOOTH-02 (S2: "заменить в пересменку 15:00"); the API serves
them (health, predictions, work order from the alert).

Isolation: database ``<db>_it_m78``, Redis DB 13 (flushed), unique channels and MQTT root.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from m3_stack import REDIS_BASE, Stack, create_database, drop_database, query, url_string
from sqlalchemy.engine import URL

from api_support import bearer
from qost_api.app import create_app
from qost_api.db import make_engine, make_sessionmaker
from qost_api.seed import run_seed
from qost_api.settings import ApiSettings
from qost_engine.replay import run_replay
from qost_sim.backfill import run_backfill
from support import REPO_ROOT
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.db.sink import DbSink
from twin_core.live import LiveKeys

pytestmark = pytest.mark.integration

REDIS_URL = REDIS_BASE.rsplit("/", 1)[0] + "/13"
RESULTS = REPO_ROOT / "var"


@pytest.fixture(scope="module")
def db_url(cfg: TwinConfig) -> Iterator[URL]:
    url = create_database("it_m78")

    async def history() -> None:
        engine = make_engine(url_string(url))
        try:
            await run_seed(
                make_sessionmaker(engine),
                cfg,
                ApiSettings(),
                around=cfg.simulation.clock.demo_start.astimezone(cfg.timezone).date(),
                now=cfg.simulation.clock.demo_start,
            )
        finally:
            await engine.dispose()
        sink = DbSink(
            url_string(url), area_of_line={ln: cfg.area_of_line(ln).code for ln in cfg.lines}
        )
        try:
            await run_backfill(cfg, sink, batch_size=5000)
        finally:
            await sink.aclose()
        await run_replay(cfg, database_url=url_string(url))

    asyncio.run(history())
    yield url
    drop_database(url)


@pytest.fixture
async def stack(db_url: URL, tmp_path: Path) -> AsyncIterator[Stack]:
    s = Stack(db_url, tmp_path, redis_url=REDIS_URL)
    try:
        yield s
    finally:
        await s.stop()


async def wait_for(
    stack: Stack, db_url: URL, sql: str, timeout_s: float = 90.0
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = await query(db_url, sql)
        if rows:
            return rows
        await asyncio.sleep(0.5)
    raise TimeoutError(f"no rows for {sql}\n{stack.log('engine')}")


async def test_s3_and_s2_through_the_engine_and_the_api(
    stack: Stack, db_url: URL, cfg: TwinConfig
) -> None:
    demo_start = cfg.simulation.clock.demo_start
    await stack.start()
    await stack.run_plant(300.0)
    # S2 → AL-M2 for BOOTH-02, S3 → AL-M1 for ABB-04
    m2 = await wait_for(
        stack,
        db_url,
        "SELECT message_ru, value, severity, status FROM alert WHERE rule_id = 'AL-M2' "
        "AND entity = 'BOOTH-02' AND status <> 'resolved'",
    )
    m1 = await wait_for(
        stack,
        db_url,
        "SELECT message_ru, value, severity FROM alert "
        "WHERE rule_id = 'AL-M1' AND entity = 'ABB-04'",
    )
    await asyncio.sleep(3.0)  # a few more ticks
    await stack.sim("POST", "/pause")
    await asyncio.sleep(1.5)
    preds = await query(
        db_url,
        "SELECT equipment, ts, p_failure, health_index, model_version, top_factors FROM prediction "
        "WHERE ts >= :t ORDER BY ts, equipment",
        t=demo_start,
    )
    abb = [p for p in preds if p["equipment"] == "ABB-04"]
    assert len(abb) >= 3
    assert {p["equipment"] for p in preds} >= {"ABB-01", "ABB-04", "JIG-01", "OVEN-01", "CONV-03"}
    assert all(p["model_version"] for p in preds)
    assert abb[-1]["top_factors"]
    assert abb[-1]["top_factors"][0]["text_ru"]
    # the S2 alert: window = the 15:00 shift change
    value = m2[0]["value"]
    assert value["signal"] == "filter_dp_pa"
    assert 6.0 <= value["hours_to_limit"] <= 13.0
    assert "заменить фильтры в пересменку 15:00" in m2[0]["message_ru"], m2[0]
    # live view: health_index in live:equipment (snapshot §12.3)
    raw = await stack.redis.hget(LiveKeys().key("equipment"), "ABB-04")
    assert raw is not None
    assert json.loads(raw)["health_index"] is not None
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "m78_live.json").write_text(
        json.dumps(
            {
                "abb04": [[p["ts"].isoformat(), p["p_failure"], p["health_index"]] for p in abb],
                "am1": m1[0],
                "am2": m2[0],
                "factors": abb[-1]["top_factors"],
            },
            indent=1,
            default=str,
            ensure_ascii=False,
        )
    )

    # ---- the API over the same database
    last = (await query(db_url, "SELECT max(ts) AS t FROM telemetry"))[0]["t"]
    app = create_app(cfg, clock=ManualClock(last), database_url=url_string(db_url))
    with TestClient(app) as client:
        r = client.get("/api/v1/equipment/ABB-04/health", headers=bearer("maintenance"))
        assert r.status_code == 200, r.text
        health = r.json()
        assert health["prediction"]["p_failure"] == pytest.approx(abb[-1]["p_failure"], abs=1e-6)
        assert health["prediction"]["factors"]
        assert health["health_index"] is not None
        r = client.get("/api/v1/equipment/BOOTH-02/health", headers=bearer("maintenance"))
        limits = r.json()["limits"]
        assert limits
        assert limits[0]["signal"] == "filter_dp_pa"
        r = client.get(
            "/api/v1/predictions", params={"latest": "true"}, headers=bearer("maintenance")
        )
        assert r.status_code == 200
        latest = {i["equipment"]: i for i in r.json()["items"]}
        assert "ABB-04" in latest
        assert latest["ABB-04"]["factors"]
        r = client.get(
            "/api/v1/predictions",
            params={"equipment": "ABB-04", "limit": 2},
            headers=bearer("director"),
        )
        page = r.json()
        assert len(page["items"]) == 2
        assert page["next_cursor"]
        r2 = client.get(
            "/api/v1/predictions",
            params={"equipment": "ABB-04", "limit": 2, "cursor": page["next_cursor"]},
            headers=bearer("director"),
        )
        assert r2.json()["items"][0]["ts"] < page["items"][-1]["ts"]
        # work order from the AL-M2 alert with prefill; a second one is a conflict
        alert = (
            await query(
                db_url, "SELECT id FROM alert WHERE rule_id = 'AL-M2' AND entity = 'BOOTH-02'"
            )
        )[0]["id"]
        created = client.post(
            "/api/v1/work-orders", json={"alert_id": alert}, headers=bearer("maintenance")
        )
        assert created.status_code == 201, created.text
        order = created.json()
        assert order["equipment"] == "BOOTH-02"
        assert order["kind"] == "predictive"
        assert order["due_ts"]
        assert order["status"] == "open"
        dup = client.post("/api/v1/work-orders", json={"alert_id": alert}, headers=bearer("master"))
        assert dup.status_code == 409
        assert dup.json()["type"] == "/problems/work-order-exists"
        patched = client.patch(
            f"/api/v1/work-orders/{order['id']}",
            json={"status": "in_progress", "assignee": "maintenance"},
            headers=bearer("maintenance"),
        )
        assert patched.status_code == 200
        assert patched.json()["assignee"] == "maintenance"
        listed = client.get(
            "/api/v1/work-orders", params={"status": "in_progress"}, headers=bearer("master")
        )
        assert [o["id"] for o in listed.json()["items"]] == [order["id"]]
        closed = client.patch(
            f"/api/v1/work-orders/{order['id']}", json={"status": "done"}, headers=bearer("admin")
        )
        assert closed.json()["closed_ts"]
        assert (
            client.patch(
                f"/api/v1/work-orders/{order['id']}",
                json={"status": "open"},
                headers=bearer("admin"),
            ).status_code
            == 409
        )
        # quality: SPC p-chart, Pareto, correlations, body trace
        r = client.get("/api/v1/quality/spc", params={"area": "PAINT"}, headers=bearer("quality"))
        assert r.status_code == 200, r.text
        (chart,) = r.json()["areas"]
        assert chart["area"] == "PAINT"
        assert chart["points"]
        assert chart["p_bar"] is not None
        assert chart["baseline_shifts"] == 20
        assert {"key", "p", "ucl", "lcl", "rules"} <= set(chart["points"][0])
        r = client.get("/api/v1/quality/pareto", headers=bearer("director"))
        pareto = r.json()
        assert pareto["total"] > 0
        assert pareto["items"][0]["cumulative"] <= 1.0
        assert pareto["items"][0]["share"] >= pareto["items"][-1]["share"]
        r = client.get("/api/v1/quality/correlations", headers=bearer("master"))
        corr = r.json()
        assert corr["area"] == "PAINT"
        assert corr["hours"] > 100
        assert {i["factor"] for i in corr["insights"]} == {"filter_dp_pa", "humidity_pct_deviation"}
        body_id = (
            await query(db_url, "SELECT body_id FROM unit_event WHERE body_id IS NOT NULL LIMIT 1")
        )[0]["body_id"]
        r = client.get(f"/api/v1/bodies/{body_id}", headers=bearer("quality"))
        assert r.status_code == 200, r.text
        assert r.json()["events"]
        assert client.get("/api/v1/bodies/NOPE", headers=bearer("master")).status_code == 404
        assert client.get("/api/v1/bodies/x", headers=bearer("director")).status_code == 403
    audit = await query(db_url, "SELECT action FROM audit_log WHERE action LIKE 'work_order.%'")
    assert {a["action"] for a in audit} == {"work_order.create", "work_order.update"}
