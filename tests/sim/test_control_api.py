"""Demo console HTTP API of the simulator (SPEC §6.9)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2
import pytest

from qost_sim.bus import MemoryBus
from qost_sim.control_api import create_app
from qost_sim.live import LiveRunner
from qost_sim.settings import SimSettings
from twin_core.config import TwinConfig


@pytest.fixture
async def runner(cfg: TwinConfig) -> LiveRunner:
    return LiveRunner(cfg, SimSettings(sim_reset_flush_s=0), bus=MemoryBus())


@pytest.fixture
async def client(runner: LiveRunner) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_app(runner))
    async with httpx2.AsyncClient(transport=transport, base_url="http://sim") as http:
        yield http


async def test_health_and_readiness(runner: LiveRunner, client: httpx2.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok", "service": "sim"}
    not_ready = await client.get("/readyz")
    assert not_ready.status_code == 503
    assert (await client.post("/pause")).status_code == 409
    await runner.start_up()
    assert (await client.get("/readyz")).status_code == 200


async def test_console_commands(runner: LiveRunner, client: httpx2.AsyncClient) -> None:
    await runner.start_up()
    status = (await client.get("/status")).json()
    assert {
        "plant_time",
        "plant_time_local",
        "speed",
        "paused",
        "mode",
        "scenarios",
        "lines",
    } <= set(status)
    assert status["mode"] == "live"
    scenarios = (await client.get("/scenarios")).json()
    assert [s["id"] for s in scenarios][:2] == ["S1-CHAIN-BREAK", "S2-FILTER-TREND"]

    assert (await client.post("/pause")).json()["paused"] is True
    assert (await client.post("/start")).json()["paused"] is False
    assert (await client.post("/speed", json={"value": 10})).json()["speed"] == 10
    assert (await client.post("/speed", json={"value": 0})).status_code == 422
    assert (await client.post("/speed", json={"value": 1000})).status_code == 422
    assert (await client.post("/speed", json={"valu": 1})).status_code == 422

    s1 = await client.post("/inject", json={"scenario_id": "S1-CHAIN-BREAK"})
    assert s1.status_code == 200
    assert s1.json()["scenario_id"] == "S1-CHAIN-BREAK"
    assert s1.json()["status"]["lines"]["ASSY-1"]["state"] == "DOWN_UNPLANNED"
    assert (await client.post("/inject", json={"scenario_id": "S9"})).status_code == 404
    mixed = await client.post("/inject", json={"scenario_id": "S1-CHAIN-BREAK", "type": "ckd"})
    assert mixed.status_code == 422
    bad = await client.post(
        "/inject",
        json={
            "type": "failure",
            "equipment": "CONV-03",
            "reason": "PM-CLEANING",
            "duration_min": 5,
        },
    )
    assert bad.status_code == 422
    assert "planned" in bad.json()["detail"][0]
    adhoc = await client.post("/inject", json={"type": "ckd", "product": "J7", "set_kits": 5})
    assert adhoc.status_code == 200
    assert adhoc.json()["status"]["kits"]["J7"] == 5

    assert (await client.post("/reset", json={"to": "now"})).status_code == 422
    reset = await client.post("/reset", json={"to": "demo_start"})
    assert reset.status_code == 200
    assert reset.json()["cleanup"] == "no_listeners"
    assert reset.json()["status"]["epoch"] == 1
    assert (await client.post("/reset")).status_code == 200
