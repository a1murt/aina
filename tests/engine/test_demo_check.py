"""tools/demo_check.py (STAB-5): the pre-show checks turn a frozen engine into a readable FAIL."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

from support import REPO_ROOT


@pytest.fixture(scope="module")
def dc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_check", REPO_ROOT / "tools/demo_check.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_check"] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


DEMO = "2026-10-16T02:00:00.000000Z"
WALL = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def sim(paused: bool = True, plant: str = DEMO, epoch: int = 16, last: str = "acked") -> dict:  # type: ignore[type-arg]
    local = datetime.fromisoformat(plant.replace("Z", "+00:00")) + timedelta(hours=5)
    return {
        "paused": paused,
        "demo_start": DEMO,
        "plant_time": plant,
        "plant_time_local": local.replace(tzinfo=None).isoformat() + "+05:00",
        "epoch": epoch,
        "last_reset": last,
    }


def test_health_lines(dc: ModuleType) -> None:
    assert dc.check_health("engine", 200, {}, None).status == "OK"
    paused = dc.check_health("engine", None, None, "timed out")
    assert paused.status == "FAIL"
    assert "restart engine" in paused.text
    stalled = dc.check_health("engine", 503, {"not_ready": ["consume_stale:20s"]}, None)
    assert stalled.status == "FAIL"
    assert "consume_stale" in stalled.text


def test_engine_progress_and_lag(dc: ModuleType) -> None:
    alive = {"consume": True, "timers": True}
    before = {"processed": 100, "tasks_alive": alive}
    grown = {"processed": 160, "tasks_alive": alive}
    assert [line.status for line in dc.check_engine(True, before, grown, 0, 5.0)] == ["OK"]
    frozen = dc.check_engine(True, before, before, 812, 5.0)
    assert frozen[0].status == "FAIL"
    assert "processed не растёт" in frozen[0].text
    assert dc.check_engine(False, before, before, 0, 0.0)[0].status == "OK"
    assert dc.check_engine(False, before, before, 40, 0.0)[0].status == "FAIL"
    dead = dc.check_engine(False, before, {**before, "tasks_alive": {"timers": False}}, 0, 0.0)
    assert dead[0].status == "FAIL"
    assert "timers" in dead[0].text
    assert dc.check_engine(True, None, None, None, 0.0)[0].status == "FAIL"


def test_clock_epoch_collector_plan(dc: ModuleType) -> None:
    fresh = {"wall_ts": WALL.isoformat()}
    ready = dc.check_clock(sim(), fresh, WALL, max_drift_h=10)
    assert [(line.status, line.text) for line in ready] == [
        ("OK", "16.10 07:00, пауза — готов к показу")
    ]
    drifted = dc.check_clock(sim(plant="2026-11-12T13:10:00Z"), fresh, WALL, max_drift_h=10)
    assert drifted[0].status == "FAIL"
    assert "12.11 18:10" in drifted[0].text
    stale = dc.check_clock(sim(paused=False), fresh, WALL + timedelta(seconds=30), max_drift_h=10)
    assert stale[0].status == "FAIL"
    assert dc.check_epoch(sim(), {"epoch": 16}).status == "OK"
    assert dc.check_epoch(sim(), {"epoch": 15}).status == "FAIL"
    assert dc.check_epoch(sim(last="timeout"), {"epoch": 16}).status == "FAIL"
    assert dc.check_collector({"queued": "0", "spooled": "0"}).status == "OK"
    assert dc.check_collector({"queued": "0", "spooled": "12"}).status == "FAIL"
    progress = {"month": "2026-10", "mtd_output": 2511, "targets": {"plant_target": {}}}
    assert dc.check_plan(progress, at_start=True, mtd_expect=2511, mtd_tol=0.02).status == "OK"
    assert dc.check_plan(progress, at_start=True, mtd_expect=3000, mtd_tol=0.02).status == "FAIL"
    no_plan = {"month": "2026-11", "mtd_output": 0, "targets": {}}
    assert dc.check_plan(no_plan, at_start=False, mtd_expect=None, mtd_tol=0.02).status == "FAIL"
