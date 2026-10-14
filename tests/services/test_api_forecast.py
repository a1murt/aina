"""Forecast API without a database (in-memory backend over virtual-plant history): roles,
RFC 7807 problems, result shape, FR-FC-01 through the API, calibration snapshot reuse.

Persistence in TimescaleDB is tested in tests/integration/test_forecast_api.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from api_support import bearer
from forecast_support import MemoryBackend, demo_history, memory_backend
from qost_api.app import create_app
from qost_api.auth import Principal
from qost_api.forecast.service import EffectRequest, ForecastService
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig

PROBLEM = "application/problem+json"
DIRECTOR = bearer("director")


@pytest.fixture
def backend() -> MemoryBackend:
    return memory_backend(demo_history())


@pytest.fixture
def client(cfg: TwinConfig, backend: MemoryBackend) -> Iterator[TestClient]:
    clock = ManualClock(cfg.simulation.clock.demo_start)
    app = create_app(cfg, clock=clock, database_url=None, forecast_backend=backend)
    with TestClient(app) as test_client:
        yield test_client


def test_without_database_forecast_is_unavailable(cfg: TwinConfig) -> None:
    with TestClient(create_app(cfg, database_url=None)) as c:
        response = c.post("/api/v1/forecast", json={}, headers=DIRECTOR)
    assert response.status_code == 503
    assert response.json()["type"] == "/problems/no-database"


def test_post_forecast_returns_the_director_view(
    client: TestClient, backend: MemoryBackend
) -> None:
    started = time.perf_counter()
    response = client.post(
        "/api/v1/forecast",
        json={
            "overrides": {"defect_rate": {"PAINT": 0.03}, "extra_shifts": [{"date": "2026-10-17"}]}
        },
        headers=DIRECTOR,
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == 1
    assert body["status"] == "done"
    assert body["n_runs"] == 5000
    result = body["result"]
    assert result["month"] == "2026-10"
    assert result["targets"] == {"plant_target": 5500, "line_plan": 4800}
    assert set(result["p_reach"]) == {"plant_target", "line_plan"}
    assert result["summary"]["p10"] <= result["summary"]["p50"] <= result["summary"]["p90"]
    assert result["fan"][0]["date"] == "2026-10-16"
    assert result["base"] is not None
    assert result["delta"]["p50"] > 0
    assert result["horizon"]["extra_shifts"] == [
        {"date": "2026-10-17", "shift": "A"},
        {"date": "2026-10-17", "shift": "B"},
    ]
    assert result["calibration_id"] == 1
    assert elapsed < 3.0  # base + scenario, 5 000 runs (the model itself: FR-FC-01 test)
    stored = backend.runs[1].record
    assert stored.overrides == {
        "defect_rate": {"PAINT": 0.03},
        "extra_shifts": [{"date": "2026-10-17"}],
    }
    got = client.get("/api/v1/forecast/1", headers=DIRECTOR)
    assert got.status_code == 200
    assert got.json()["result"]["summary"] == result["summary"]


def test_baseline_is_cached_between_what_ifs(client: TestClient, backend: MemoryBackend) -> None:
    first = client.post("/api/v1/forecast", json={"n_runs": 500}, headers=DIRECTOR).json()
    second = client.post(
        "/api/v1/forecast",
        json={"n_runs": 500, "overrides": {"mtbf_multiplier": {"A": 1.5}}},
        headers=DIRECTOR,
    ).json()
    assert first["result"]["base"] is None
    assert second["result"]["base"]["summary"] == first["result"]["summary"]
    assert backend.input_calls == 1  # one calibration snapshot for the day
    assert len(backend.snapshots) == 1


@pytest.mark.parametrize("role", ["operator", "master", "quality", "maintenance"])
def test_forecast_roles(client: TestClient, role: str) -> None:
    response = client.post("/api/v1/forecast", json={}, headers=bearer(role))
    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM


def test_calibration_roles_and_shape(client: TestClient) -> None:
    ok = client.get("/api/v1/calibration", headers=bearer("maintenance"))
    assert ok.status_code == 200
    body = ok.json()
    assert body["window_days"] == 20
    params = body["params"]
    assert params["equipment"]["CONV-03"]["failures"]["source"] in ("data", "prior")
    assert set(params["areas"]) == {"WELD", "PAINT", "ASSY", "QC"}
    assert client.get("/api/v1/calibration", headers=bearer("operator")).status_code == 403
    again = client.get("/api/v1/calibration", headers=bearer("admin")).json()
    assert again["id"] == body["id"]


def test_validation_problems(client: TestClient) -> None:
    bad = client.post(
        "/api/v1/forecast",
        json={"overrides": {"mtbf_multiplier": {"CONV-3": 2}}},
        headers=DIRECTOR,
    )
    assert bad.status_code == 422
    assert bad.headers["content-type"] == PROBLEM
    problem = bad.json()
    assert problem["type"] == "/problems/validation"
    assert problem["errors"][0]["loc"] == ["body", "overrides", "mtbf_multiplier", "CONV-3"]
    assert "CONV-03" in problem["errors"][0]["msg"]
    schema = client.post(
        "/api/v1/forecast", json={"overrides": {"defect_rate": {"PAINT": 2}}}, headers=DIRECTOR
    )
    assert schema.status_code == 422
    assert schema.json()["type"] == "/problems/validation"
    runs = client.post("/api/v1/forecast", json={"n_runs": 10**6}, headers=DIRECTOR)
    assert runs.status_code == 422
    assert runs.json()["errors"][0]["loc"] == ["body", "n_runs"]
    past = client.post("/api/v1/forecast", json={"month": "2026-09"}, headers=DIRECTOR)
    assert past.status_code == 422
    assert past.json()["type"] == "/problems/forecast-month"


def test_des_mode_is_not_implemented(client: TestClient) -> None:
    response = client.post("/api/v1/forecast", json={"mode": "des"}, headers=DIRECTOR)
    assert response.status_code == 501
    body = response.json()
    assert body["type"] == "/problems/not-implemented"
    assert "M9" in body["detail"]


def test_missing_run_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/forecast/999", headers=DIRECTOR)
    assert response.status_code == 404
    assert response.json()["type"] == "/problems/not-found"


def test_levers_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/forecast/levers?n_runs=100", headers=DIRECTOR)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["levers"][0]["rank"] == 1
    assert sum(1 for lever in body["levers"] if lever["top"]) == 5
    needed = body["shifts_needed_for_target"]["plant_target"]
    assert set(needed) == {"saturdays", "weekends_and_holidays"}
    cached = client.get("/api/v1/forecast/levers?n_runs=100", headers=DIRECTOR).json()
    assert cached == body
    denied = client.get("/api/v1/forecast/levers", headers=bearer("maintenance"))
    assert denied.status_code == 403


def test_effect_endpoint(client: TestClient) -> None:
    response = client.post(
        "/api/v1/effect",
        json={"n_runs": 300, "assumptions": {"margin_rate": 0.08}},
        headers=DIRECTOR,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon"] == "full_month"
    assert body["year_kzt"]["mean"] == pytest.approx(body["month_kzt"]["mean"] * 12, rel=1e-6)
    assert body["show_revenue"] is False
    margin = next(a for a in body["assumptions"] if a["key"] == "margin_rate")
    assert margin == {
        "key": "margin_rate",
        "value": 0.08,
        "assumption": True,
        "source": None,
        "note": margin["note"],
        "overridden": True,
    }
    bad = client.post(
        "/api/v1/effect", json={"n_runs": 300, "assumptions": {"revenue": 1}}, headers=DIRECTOR
    )
    assert bad.status_code == 422
    assert client.post("/api/v1/effect", json={}, headers=bearer("quality")).status_code == 403


# --------------------------------------------------------------------------- caching (S6)


def _counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """Count calls of the forecast service's ``levers`` / ``run_effect``."""
    import qost_api.forecast.service as module

    calls: list[int] = []
    real = getattr(module, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


async def test_concurrent_levers_compute_once_and_minutes_are_cached(
    cfg: TwinConfig, backend: MemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    computed = _counting(monkeypatch, "levers")
    clock = ManualClock(cfg.simulation.clock.demo_start)
    service = ForecastService(cfg, clock, backend)
    who = Principal("director", "director", 1)
    six = await asyncio.gather(*(service.levers(None, who, n_runs=100) for _ in range(6)))
    assert len(computed) == 1, "six windows opened together cost one computation"
    assert all(r == six[0] for r in six)
    started = time.perf_counter()
    again = await service.levers(None, who, n_runs=100)
    assert time.perf_counter() - started < 0.3
    assert again == six[0]
    assert len(computed) == 1

    # two plant minutes later: the recent result at once, one refresh in the background
    clock.advance(timedelta(minutes=2))
    stale = await service.levers(None, who, n_runs=100)
    assert stale == six[0]
    await asyncio.gather(*service._refresh.values())
    assert len(computed) == 2

    # a reset (plant time goes back): new epoch, nothing old is served
    clock.set(clock.now() - timedelta(hours=1))
    await service.levers(None, who, n_runs=100)
    assert service.epoch == 1
    assert len(computed) == 3


async def test_effect_is_cached_and_warm_fills_the_director_caches(
    cfg: TwinConfig, backend: MemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    levers_calls = _counting(monkeypatch, "levers")
    effect_calls = _counting(monkeypatch, "run_effect")
    service = ForecastService(cfg, ManualClock(cfg.simulation.clock.demo_start), backend)
    who = Principal("system", "admin", None)
    durations = await service.warm(who)
    assert set(durations) == {"levers_ms", "effect_ms"}
    assert (len(levers_calls), len(effect_calls)) == (1, 1)
    director = Principal("director", "director", 1)
    await service.levers(None, director)
    await service.effect(EffectRequest(), director)
    await service.effect(EffectRequest(), director)
    assert (len(levers_calls), len(effect_calls)) == (1, 1)
    other = await service.effect(EffectRequest(assumptions={"margin_rate": 0.08}), director)
    assert len(effect_calls) == 2
    assert other.month


async def test_warm_loop_on_start_pause_and_reset() -> None:
    from types import SimpleNamespace

    from qost_api.forecast.warm import warm_loop

    class Stub:
        epoch = 0

        def __init__(self) -> None:
            self.warmed = 0
            self.reset_next = False

        def observe(self, now: Any) -> bool:
            reset, self.reset_next = self.reset_next, False
            return reset

        async def warm(self, principal: Principal) -> dict[str, int]:
            self.warmed += 1
            return {"levers_ms": 1, "effect_ms": 1}

    stub = Stub()
    clock = SimpleNamespace(now=lambda: None, state=SimpleNamespace(paused=False))
    task = asyncio.create_task(
        warm_loop(cast(Any, stub), cast(Any, clock), settle_s=0.0, poll_s=0.01)
    )
    try:
        await asyncio.sleep(0.05)
        assert stub.warmed == 1  # on start
        clock.state.paused = True
        await asyncio.sleep(0.05)
        assert stub.warmed == 2  # on pause
        await asyncio.sleep(0.05)
        assert stub.warmed == 2  # once per pause
        stub.reset_next = True
        await asyncio.sleep(0.05)
        assert stub.warmed == 3  # after a reset
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
