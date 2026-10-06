"""Clock abstraction (SPEC §5.2): system, sim (Redis plant:clock) and manual clocks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from twin_core.clock import (
    PLANT_CLOCK_KEY,
    ClockNotReadyError,
    ClockState,
    ClockStateError,
    FixedClock,
    ManualClock,
    SimClock,
    SystemClock,
    create_clock,
    publish_clock_state,
    read_clock_state,
    to_plant_tz,
)
from twin_core.settings import TwinSettings

T0 = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)  # 07:00 plant time
WALL0 = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


class FakeKV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


class FakeWall:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_system_clock_is_aware_utc() -> None:
    clock = SystemClock()
    first, second = clock.now(), clock.now()
    assert first.tzinfo is UTC
    assert second >= first
    assert clock.mode == "system"
    assert clock.speed == 1.0


def test_manual_clock() -> None:
    clock = ManualClock(datetime(2026, 10, 16, 7, 0, tzinfo=timezone(timedelta(hours=5))))
    assert clock.now() == T0
    assert clock.now().tzinfo is UTC
    assert clock.advance(90) == T0 + timedelta(seconds=90)
    clock.set(T0)
    assert clock.advance(timedelta(minutes=1)) == T0 + timedelta(minutes=1)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="naive"):
        clock.set(datetime(2026, 10, 16, 7, 0))  # noqa: DTZ001 - deliberately naive
    assert FixedClock is ManualClock


def test_to_plant_tz_is_utc_plus_5() -> None:
    """Asia/Qostanay is UTC+5 since 2024-03-01 (requires tzdata >= 2024a)."""
    local = to_plant_tz(T0, "Asia/Qostanay")
    assert local.isoformat() == "2026-10-16T07:00:00+05:00"
    assert ManualClock(T0).now_in("Asia/Qostanay").hour == 7


def test_clock_state_json_format() -> None:
    state = ClockState(plant_time=T0, wall_ts=WALL0, speed=60.0, sim_mode="live")
    payload = json.loads(state.to_json())
    assert payload == {
        "v": 1,
        "plant_time": "2026-10-16T02:00:00.000000Z",
        "wall_ts": "2026-10-06T20:00:00.000000Z",
        "speed": 60.0,
        "paused": False,
        "sim_mode": "live",
    }
    assert ClockState.from_json(state.to_json()) == state
    assert ClockState.from_json(state.to_json().encode()) == state


GOOD_TS = '"plant_time": "2026-10-16T02:00:00Z", "wall_ts": "2026-10-16T02:00:00Z"'


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"v": 2, ' + GOOD_TS + ', "speed": 1}',  # unknown format version
        '{"v": 1, "wall_ts": "2026-10-16T02:00:00Z", "speed": 1}',  # plant_time missing
        '{"v": 1, "plant_time": "2026-10-16T02:00:00", "wall_ts": "2026-10-16T02:00:00Z"}',
        '{"v": 1, "plant_time": "2026-10-16T02:00:00", "wall_ts": "2026-10-16T02:00:00Z", '
        '"speed": 1}',  # naive plant_time
        '{"v": 1, ' + GOOD_TS + ', "speed": 0}',
    ],
)
def test_clock_state_rejects_malformed_values(raw: str) -> None:
    with pytest.raises(ClockStateError):
        ClockState.from_json(raw)


def test_extrapolation_between_writes() -> None:
    state = ClockState(plant_time=T0, wall_ts=WALL0, speed=60.0)
    assert state.plant_time_at(WALL0 + timedelta(seconds=1)) == T0 + timedelta(minutes=1)
    assert state.plant_time_at(WALL0 - timedelta(seconds=5)) == T0  # writer ahead: clamp
    paused = ClockState(plant_time=T0, wall_ts=WALL0, speed=60.0, paused=True)
    assert paused.plant_time_at(WALL0 + timedelta(hours=1)) == T0
    moved = state.advanced_to(WALL0 + timedelta(seconds=10))
    assert (moved.plant_time, moved.wall_ts) == (
        T0 + timedelta(minutes=10),
        WALL0 + timedelta(seconds=10),
    )


async def test_sim_clock_reads_redis_state() -> None:
    kv, wall = FakeKV(), FakeWall(WALL0)
    clock = SimClock(kv, wall=wall)
    with pytest.raises(ClockNotReadyError):
        clock.now()
    assert await clock.refresh() is None
    assert clock.is_stale

    await publish_clock_state(kv, ClockState(plant_time=T0, wall_ts=WALL0, speed=60.0))
    assert PLANT_CLOCK_KEY in kv.data
    await clock.refresh()
    assert clock.now() == T0
    wall.now = WALL0 + timedelta(seconds=5)
    assert clock.now() == T0 + timedelta(minutes=5)
    assert clock.speed == 60.0
    assert not bool(clock.is_stale)  # bool(): keep mypy from narrowing the property
    wall.now = WALL0 + timedelta(seconds=30)  # sim stopped writing for > stale_after (10 s)
    assert clock.is_stale
    assert clock.now() == T0 + timedelta(minutes=30)

    await publish_clock_state(kv, ClockState(plant_time=T0, wall_ts=WALL0, speed=60.0, paused=True))
    await clock.refresh()
    assert clock.now() == T0
    assert clock.speed == 0.0
    assert (await read_clock_state(kv)) == clock.state


async def test_sim_clock_polls_in_background() -> None:
    kv, wall = FakeKV(), FakeWall(WALL0)
    await publish_clock_state(kv, ClockState(plant_time=T0, wall_ts=WALL0, speed=1.0))
    async with SimClock(kv, wall=wall, poll_interval_s=0.01) as clock:
        assert clock.now() == T0
        later = T0 + timedelta(hours=3)
        await publish_clock_state(kv, ClockState(plant_time=later, wall_ts=WALL0, speed=1.0))
        for _ in range(100):
            if clock.now() == later:
                break
            await asyncio.sleep(0.01)
        assert clock.now() == later


async def test_sim_clock_wait_ready_timeout() -> None:
    clock = SimClock(FakeKV(), poll_interval_s=0.01)
    with pytest.raises(ClockNotReadyError, match="did not appear"):
        await clock.wait_ready(timeout_s=0.05)


def test_create_clock_by_mode() -> None:
    assert isinstance(create_clock(TwinSettings(clock_mode="system")), SystemClock)
    sim = create_clock(TwinSettings(clock_mode="sim"), store=FakeKV())
    assert isinstance(sim, SimClock)
    assert sim.mode == "sim"
