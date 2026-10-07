"""Live mode: pacing, plant clock in Redis (M0 format), pause/speed/inject/reset, restart resume."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from qost_sim.bus import MemoryBus
from qost_sim.live import LiveRunner, RunnerError, warm_model
from qost_sim.settings import SimSettings
from twin_core.clock import PLANT_CLOCK_KEY, ClockState
from twin_core.config import TwinConfig
from twin_core.config.simulation import SetStateInject
from twin_core.domain import EquipmentState

WALL = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_runner(
    cfg: TwinConfig, bus: MemoryBus | None = None, **settings: object
) -> tuple[LiveRunner, FakeMonotonic, MemoryBus]:
    mono = FakeMonotonic()
    bus = bus or MemoryBus()
    runner = LiveRunner(
        cfg,
        SimSettings(sim_reset_flush_s=0, **settings),  # type: ignore[arg-type]
        bus=bus,
        monotonic=mono,
        wall=lambda: WALL,
    )
    return runner, mono, bus


def clock(bus: MemoryBus) -> ClockState:
    return ClockState.from_json(bus.data[PLANT_CLOCK_KEY])


async def tick(runner: LiveRunner) -> None:
    async with runner._lock:
        await runner._advance()


async def advance(runner: LiveRunner, mono: FakeMonotonic, seconds: float) -> None:
    """Wall time passes in ticks of at most 5 s (the catch-up cap)."""
    while seconds > 0:
        step = min(seconds, 5.0)
        mono.advance(step)
        await tick(runner)
        seconds -= step


async def test_pacing_pause_speed_and_clock(cfg: TwinConfig) -> None:
    runner, mono, bus = make_runner(cfg)
    demo = cfg.simulation.clock.demo_start
    await runner.start_up()
    assert runner.state == "running"
    assert runner.ready
    assert runner.plant_time == demo
    state = clock(bus)
    assert (state.plant_time, state.speed, state.paused, state.sim_mode) == (
        demo,
        60.0,
        False,
        "live",
    )
    assert state.wall_ts == WALL

    await advance(runner, mono, 10)
    assert runner.plant_time == demo + timedelta(minutes=10)
    assert runner.status()["shift"] == {"date": "2026-10-16", "code": "A"}
    assert runner.shift_label() == "2026-10-16/A"

    await runner.pause()
    assert clock(bus).paused
    mono.advance(100)
    async with runner._lock:
        await runner._publish_clock(force=True)
    assert runner.plant_time == demo + timedelta(minutes=10)
    await runner.start()
    mono.advance(1)
    await tick(runner)
    assert runner.plant_time == demo + timedelta(minutes=11)

    await runner.set_speed(300)
    await advance(runner, mono, 2)
    assert runner.plant_time == demo + timedelta(minutes=21)
    assert clock(bus).speed == 300.0
    for bad in (0, -1, 301):
        with pytest.raises(ValueError, match="speed"):
            await runner.set_speed(bad)

    mono.advance(1000)  # far behind: catch-up is capped and re-anchored
    await tick(runner)
    assert runner.plant_time == demo + timedelta(minutes=21, seconds=300 * 5)


async def test_inject_reset_and_persisted_state(cfg: TwinConfig) -> None:
    runner, mono, bus = make_runner(cfg)
    await runner.start_up()
    mono.advance(60)
    await tick(runner)
    at = await runner.inject(cfg.scenarios["S1-CHAIN-BREAK"].inject, scenario_id="S1-CHAIN-BREAK")
    assert timedelta(0) <= runner.plant_time - at < timedelta(milliseconds=1)
    assert runner.model is not None
    assert runner.model.units["CONV-03"].state is EquipmentState.DOWN_UNPLANNED
    status = runner.status()
    s1 = [s for s in status["scenarios"] if s["scenario_id"] == "S1-CHAIN-BREAK"]
    assert s1[0]["status"] == "active"
    assert {s["scenario_id"] for s in status["scenarios"]} >= {"S2-FILTER-TREND", "S3-ABB04-WEAR"}
    saved = json.loads(bus.data["sim:state"])
    assert saved["epoch"] == 0
    assert saved["interventions"][0]["scenario_id"] == "S1-CHAIN-BREAK"
    json.dumps(status)  # the console gets plain JSON

    await runner.pause()
    bad = SetStateInject.model_validate(
        {"type": "set_state", "equipment": "CONV-03", "vibration_mm_s": 9}
    )
    with pytest.raises(ValueError, match="cannot be set"):
        await runner.inject(bad)

    outcome = await runner.reset()
    assert outcome == "no_listeners"
    assert bus.resets[-1]["epoch"] == 1
    assert bus.resets[-1]["action"] == "reset"
    assert runner.epoch == 1
    assert runner.state == "paused"  # keeps the state it had
    assert runner.plant_time == cfg.simulation.clock.demo_start
    assert runner.manual == []
    assert json.loads(bus.data["sim:state"]) == {"v": 1, "epoch": 1, "interventions": []}
    assert runner.model is not None
    assert runner.model.units["CONV-03"].state is not EquipmentState.DOWN_UNPLANNED


async def test_reset_waits_for_the_cleanup_ack(cfg: TwinConfig) -> None:
    seen: list[int] = []

    def engine(message: dict[str, object]) -> bool:
        seen.append(int(message["epoch"]))  # type: ignore[call-overload]
        return True

    runner, _mono, _bus = make_runner(cfg, MemoryBus(on_reset=engine))
    await runner.start_up()
    assert await runner.reset() == "acked"
    assert seen == [1]
    assert runner.state == "running"


async def test_restart_resumes_at_the_last_plant_time(cfg: TwinConfig) -> None:
    demo = cfg.simulation.clock.demo_start
    s1 = cfg.scenarios["S1-CHAIN-BREAK"].inject
    bus = MemoryBus()
    previous = ClockState(demo + timedelta(hours=2), WALL, 60.0, paused=False, sim_mode="live")
    bus.data[PLANT_CLOCK_KEY] = previous.to_json()
    bus.data["sim:state"] = json.dumps(
        {
            "v": 1,
            "epoch": 3,
            "interventions": [
                {
                    "at": (demo + timedelta(hours=1)).isoformat(),
                    "scenario_id": "S1-CHAIN-BREAK",
                    "inject": s1.model_dump(mode="json"),
                }
            ],
        }
    )
    runner, _mono, _ = make_runner(cfg, bus)
    await runner.start_up()
    assert runner.epoch == 3
    assert runner.plant_time == demo + timedelta(hours=2)
    reference = warm_model(cfg, interventions=[(demo + timedelta(hours=1), s1, "S1-CHAIN-BREAK")])
    reference.run_until_time(demo + timedelta(hours=2))
    assert runner.model is not None
    got, want = runner.model.snapshot(), reference.snapshot()
    for snap in (got, want):
        for unit in snap["units"].values():
            unit["values"] = {}  # live samples telemetry, the reference does not
    assert got == want

    fresh, _, bus2 = make_runner(cfg, MemoryBus(), sim_resume=False)
    bus2.data.update(bus.data)
    await fresh.start_up()
    assert fresh.epoch == 4  # not resumed: a new epoch (new event ids)
    assert fresh.plant_time == demo


async def test_commands_need_a_ready_simulator(cfg: TwinConfig) -> None:
    runner, _mono, _bus = make_runner(cfg, sim_autostart=False)
    with pytest.raises(RunnerError, match="starting"):
        await runner.pause()
    assert runner.status()["state"] == "starting"
    await runner.start_up()
    assert runner.state == "paused"
    assert clock(_bus).paused
