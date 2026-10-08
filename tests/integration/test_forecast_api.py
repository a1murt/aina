"""Forecast against TimescaleDB: the database adapter reads the same facts the virtual plant
produced, and runs, snapshots and audit records are persisted (SPEC §8, §10, §12.2).

Uses its own database ``<TEST_DATABASE_URL db>_it_m6`` (dropped and re-created for the module,
migrated to head). Rows are written in the §8 shapes the engine produces: state intervals,
downtime (with microstop/planned flags), unit events, the latest buffer levels, CKD stock and
filter pressure drop.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forecast_support import History, demo_history
from qost_api.app import create_app
from qost_api.forecast.data import load_calibration_inputs, load_plant_state, load_targets
from qost_sim.forecast_inputs import state_intervals
from support import REPO_ROOT
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState
from twin_core.forecast.calibration import CalibrationInputs, calibration_window
from twin_core.forecast.params import PlantState

pytestmark = pytest.mark.integration

BASE_URL = make_url(
    os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://qost:qost@localhost:5432/qost")
)
DIRECTOR = {"X-Dev-Role": "director", "X-Dev-User": "it-director"}
_DOWN = {EquipmentState.DOWN_UNPLANNED.value, EquipmentState.DOWN_PLANNED.value}


async def _admin(sql: str) -> None:
    engine = create_async_engine(BASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def query(url: URL, sql: str) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [dict(row) for row in result.mappings()] if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _rows(history: History) -> dict[str, list[dict[str, Any]]]:
    """§8 rows from the virtual-plant records (window start minus a day .. now)."""
    cfg, model = history.cfg, history.model
    now = model.env.now
    w0, _, _ = calibration_window(cfg, history.now)
    keep_from = model.sec(w0 - timedelta(days=1))
    threshold = cfg.rules.thresholds.microstop_threshold_s
    states: list[dict[str, Any]] = []
    downtime: list[dict[str, Any]] = []
    for entity_type in ("equipment", "line"):
        for entity, items in state_intervals(history.records, entity_type, until=now).items():
            for iv in items:
                end = iv.end
                if end is not None and end < keep_from:
                    continue
                states.append(
                    {
                        "entity": entity,
                        "start_ts": model.at(iv.start),
                        "end_ts": None if end is None else model.at(end),
                        "entity_type": entity_type,
                        "state": iv.state,
                        "reason_code": iv.reason,
                        "source": "sim",
                    }
                )
                if entity_type == "equipment" and iv.state in _DOWN:
                    reason = iv.reason or "UNK"
                    duration = None if end is None else end - iv.start
                    downtime.append(
                        {
                            "entity": entity,
                            "line": cfg.line_of_equipment(entity).code,
                            "start_ts": model.at(iv.start),
                            "end_ts": None if end is None else model.at(end),
                            "duration_s": duration,
                            "planned": cfg.reasons[reason].planned,
                            "microstop": duration is not None and duration < threshold,
                            "reason_code": reason,
                            "reason_source": "auto",
                        }
                    )
    units = [
        {
            "line": r.data["line"],
            "body_id": r.data["body_id"],
            "ts": model.at(r.t),
            "product": r.data["product"],
            "result": r.data["result"],
            "defect_code": r.data.get("defect_code"),
        }
        for r in history.records
        if r.kind == "unit" and r.t >= keep_from
    ]
    stamp = history.now - timedelta(minutes=1)
    state = history.state()
    buffers = [{"buffer": c, "ts": stamp, "level": int(v)} for c, v in state.buffers.items()]
    kits = [{"product": p, "ts": stamp, "kits": int(k)} for p, k in state.kits.items()]
    pf = cfg.simulation.paint_filters
    assert pf is not None
    telemetry = [
        {"equipment": c, "signal": pf.signal, "ts": stamp, "value": v, "quality": "good"}
        for c, v in state.filter_dp.items()
    ]
    return {
        "equipment_state": states,
        "downtime": downtime,
        "unit_event": units,
        "buffer_level": buffers,
        "ckd_stock": kits,
        "telemetry": telemetry,
    }


async def _load(url: URL, rows: dict[str, list[dict[str, Any]]]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for table, items in rows.items():
                if not items:
                    continue
                cols = list(items[0])
                sql = text(
                    f"INSERT INTO {table} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})"
                )
                await conn.execute(sql, items)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def history() -> History:
    return demo_history()


@pytest.fixture(scope="module")
def db_url(history: History) -> Iterator[URL]:
    name = f"{BASE_URL.database}_it_m6"
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
    asyncio.run(_load(url, _rows(history)))
    yield url
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _db_inputs(
    url: URL, cfg: TwinConfig, history: History
) -> tuple[CalibrationInputs, PlantState]:
    engine = create_async_engine(url)
    try:
        sessions = async_sessionmaker(engine)
        w0, w1, days = calibration_window(cfg, history.now)
        async with sessions() as session:
            inputs = await load_calibration_inputs(
                session, cfg, window_from=w0, window_to=w1, working_days=days, as_of=history.now
            )
            state = await load_plant_state(session, cfg, as_of=history.now)
            targets = await load_targets(session, cfg, "2026-10")
        assert targets.source == "config"
        return inputs, state
    finally:
        await engine.dispose()


def test_database_adapter_matches_the_virtual_plant(
    db_url: URL, cfg: TwinConfig, history: History
) -> None:
    db_inputs, db_state = asyncio.run(_db_inputs(db_url, cfg, history))
    sim_inputs = history.inputs()
    assert db_inputs.window_from == sim_inputs.window_from
    assert db_inputs.working_days == sim_inputs.working_days
    for code, hours in sim_inputs.operating_h.items():
        assert math.isclose(db_inputs.operating_h[code], hours, abs_tol=1e-6), code
    assert sorted(db_inputs.stops, key=lambda s: (s.equipment, s.start)) == sorted(
        sim_inputs.stops, key=lambda s: (s.equipment, s.start)
    )
    assert len(db_inputs.line_shifts) == len(sim_inputs.line_shifts)
    for got, want in zip(db_inputs.line_shifts, sim_inputs.line_shifts, strict=True):
        assert (got.line, got.shift_date, got.shift_code) == (
            want.line,
            want.shift_date,
            want.shift_code,
        )
        assert math.isclose(got.apt_s, want.apt_s, abs_tol=1e-3)
        assert math.isclose(got.degraded_s, want.degraded_s, abs_tol=1e-3)
        assert dict(got.exits) == dict(want.exits)
        assert (got.defects, got.repaint_defects) == (want.defects, want.repaint_defects)
    assert {k: dict(v) for k, v in db_inputs.defect_codes.items()} == {
        k: dict(v) for k, v in sim_inputs.defect_codes.items()
    }
    sim_state = history.state()
    assert db_state.mtd_output == sim_state.mtd_output
    assert db_state.buffers == sim_state.buffers
    assert db_state.open_downs == sim_state.open_downs
    assert db_state.kits == sim_state.kits
    assert db_state.held == sim_state.held
    assert sum(db_state.held.values()) > 0
    assert db_state.filter_dp == pytest.approx(sim_state.filter_dp)


@pytest.fixture
def client(db_url: URL, cfg: TwinConfig) -> Iterator[TestClient]:
    query(
        db_url,
        "TRUNCATE forecast_run, calibration_snapshot, audit_log, production_plan "
        "RESTART IDENTITY CASCADE",
    )
    app = create_app(
        cfg,
        clock=ManualClock(cfg.simulation.clock.demo_start),
        database_url=db_url.render_as_string(hide_password=False),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_forecast_run_is_persisted_and_audited(client: TestClient, db_url: URL) -> None:
    created = client.post(
        "/api/v1/forecast",
        json={"n_runs": 1000, "overrides": {"defect_rate": {"PAINT": 0.03}}},
        headers=DIRECTOR,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    rows = query(
        db_url, "SELECT id, mode, month, n_runs, status, overrides, duration_ms FROM forecast_run"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == body["id"]
    assert (row["mode"], row["month"], row["n_runs"], row["status"]) == (
        "fast",
        "2026-10",
        1000,
        "done",
    )
    assert row["overrides"] == {"defect_rate": {"PAINT": 0.03}}
    actions = [r["action"] for r in query(db_url, "SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["calibration.create", "forecast.run"]
    got = client.get(f"/api/v1/forecast/{body['id']}", headers=DIRECTOR)
    assert got.status_code == 200
    assert got.json()["result"] == body["result"]
    by = query(db_url, "SELECT after FROM audit_log WHERE action = 'forecast.run'")[0]["after"]
    assert by["by"] == "it-director"


def test_calibration_snapshot_once_per_window(client: TestClient, db_url: URL) -> None:
    first = client.get("/api/v1/calibration", headers={"X-Dev-Role": "maintenance"})
    assert first.status_code == 200
    second = client.get("/api/v1/calibration", headers=DIRECTOR)
    assert second.json()["id"] == first.json()["id"]
    assert query(db_url, "SELECT count(*) AS n FROM calibration_snapshot")[0]["n"] == 1


def test_snapshot_survives_a_restart(cfg: TwinConfig, db_url: URL, client: TestClient) -> None:
    first = client.get("/api/v1/calibration", headers=DIRECTOR).json()
    app = create_app(
        cfg,
        clock=ManualClock(cfg.simulation.clock.demo_start),
        database_url=db_url.render_as_string(hide_password=False),
    )
    with TestClient(app) as other:
        again = other.get("/api/v1/calibration", headers=DIRECTOR).json()
    assert again["id"] == first["id"]
    assert again["params"] == first["params"]


def test_targets_from_the_plan_table(client: TestClient, db_url: URL) -> None:
    query(
        db_url,
        "INSERT INTO production_plan (month, level, line, product, qty) "
        "VALUES ('2026-10', 'plant_target', NULL, NULL, 5600)",
    )
    body = client.post("/api/v1/forecast", json={"n_runs": 200}, headers=DIRECTOR).json()
    assert body["result"]["targets"] == {"plant_target": 5600, "line_plan": 4800}


def test_levers_and_effect_end_to_end(client: TestClient) -> None:
    levers = client.get("/api/v1/forecast/levers?n_runs=200", headers=DIRECTOR)
    assert levers.status_code == 200, levers.text
    assert levers.json()["levers"]
    effect = client.post("/api/v1/effect", json={"n_runs": 300}, headers=DIRECTOR)
    assert effect.status_code == 200, effect.text
    assert effect.json()["month_kzt"]["mean"] > 0
