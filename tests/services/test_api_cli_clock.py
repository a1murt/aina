"""Plant time of ``python -m qost_api seed-reports``: a stale clock is not extrapolated."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from qost_api.__main__ import _plant_now
from qost_api.settings import ApiSettings
from twin_core.clock import ClockState, publish_clock_state, system_now
from twin_core.config import TwinConfig


class MemoryKV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


PLANT = datetime(2026, 10, 16, 2, 30, tzinfo=UTC)


async def _now_with(state: ClockState, cfg: TwinConfig) -> datetime:
    kv = MemoryKV()
    await publish_clock_state(kv, state)
    settings = ApiSettings(clock_mode="sim", redis_enabled=False)
    return await _plant_now(cfg, settings, None, store=kv)


async def test_a_stale_running_clock_is_not_extrapolated(cfg: TwinConfig) -> None:
    # the simulator stopped two hours ago at 60x without publishing a pause (make demo-down)
    stale = ClockState(plant_time=PLANT, wall_ts=system_now() - timedelta(hours=2), speed=60.0)
    assert await _now_with(stale, cfg) == PLANT  # where the simulator resumes


async def test_a_fresh_running_clock_is_extrapolated(cfg: TwinConfig) -> None:
    fresh = ClockState(plant_time=PLANT, wall_ts=system_now() - timedelta(seconds=1), speed=60.0)
    now = await _now_with(fresh, cfg)
    assert PLANT + timedelta(seconds=30) <= now <= PLANT + timedelta(minutes=5)


async def test_no_clock_falls_back_to_demo_start(cfg: TwinConfig) -> None:
    settings = ApiSettings(clock_mode="sim", redis_enabled=False)
    now = await _plant_now(cfg, settings, None, store=MemoryKV())
    assert now == cfg.simulation.clock.demo_start
    assert await _plant_now(cfg, settings, "2026-10-15T07:00") == datetime(
        2026, 10, 15, 2, 0, tzinfo=UTC
    )
