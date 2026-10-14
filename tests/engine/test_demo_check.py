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
    # low speed: no new non-telemetry event, but lag 0 and a fresh read -> healthy
    idle = {**before, "last_consume_age_s": 0.4}
    assert dc.check_engine(True, before, idle, 0, 5.0)[0].status == "OK"
    stale = {**before, "last_consume_age_s": 42.0}
    assert dc.check_engine(True, before, stale, 0, 5.0)[0].status == "FAIL"
    assert dc.check_engine(True, before, idle, 3, 5.0)[0].status == "FAIL"
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
    assert dc.check_collector([{"queued": "0", "spooled": "0"}]).status == "OK"
    assert dc.check_collector([{"queued": "0", "spooled": "12"}]).status == "FAIL"
    progress = {"month": "2026-10", "mtd_output": 2511, "targets": {"plant_target": {}}}
    assert dc.check_plan(progress, at_start=True, mtd_expect=2511, mtd_tol=0.02).status == "OK"
    assert dc.check_plan(progress, at_start=True, mtd_expect=3000, mtd_tol=0.02).status == "FAIL"
    no_plan = {"month": "2026-11", "mtd_output": 0, "targets": {}}
    assert dc.check_plan(no_plan, at_start=False, mtd_expect=None, mtd_tol=0.02).status == "FAIL"


def _status(queued: int, spooled: int = 0, db: int = 0, stream: int = 0) -> dict[str, str]:
    return {
        "queued": str(queued),
        "spooled": str(spooled),
        "db_written": str(db),
        "stream_written": str(stream),
    }


def test_collector_queue_is_sampled_not_read_once(dc: ModuleType) -> None:
    """Right after a demo reset the collector still writes a batch (queue 24-25 in one read):
    a queue that empties or shrinks within ~2 s is healthy; only a stuck queue or a spool fail."""
    check = dc.check_collector
    # a batch written right after the reset: non-empty in the first read, empty in the next
    assert check([_status(24), _status(0)], 0.5).status == "OK"
    # draining but not empty yet
    drain = check([_status(25), _status(12), _status(3)], 2.0)
    assert (drain.status, drain.text) == ("OK", "очередь разгружается: 25 → 3 за 2 с, спул пуст")
    # a running plant: always a few events in flight, written all the time
    busy = check([_status(2, db=100), _status(3, db=180), _status(2, db=260)], 2.0)
    assert (busy.status, busy.text) == ("OK", "в очереди 2, запись идёт (+160 за 2 с)")
    flowing = check([_status(2, db=100), _status(3, db=180), _status(4, db=260)], 2.0)
    assert flowing.status == "WARN"
    assert "запись идёт (+160)" in flowing.text
    # a stuck writer: the queue stays and nothing is written
    stuck = check([_status(24, db=7), _status(24, db=7), _status(25, db=7)], 2.0)
    assert stuck.status == "FAIL"
    assert "не разгружается: 24 → 25 за 2 с" in stuck.text
    # one read only (old behaviour) with a queue: no evidence of draining → FAIL
    assert check([_status(24)]).status == "FAIL"
    # a spool always fails, even with an empty queue
    assert check([_status(0), _status(0, spooled=4096)], 1.0).status == "FAIL"
    assert check([None, {}]).status == "WARN"


def test_collector_sampling_stops_early_and_spans_the_window(dc: ModuleType) -> None:
    now = [0.0]
    reads = iter([_status(25), _status(9), _status(0), _status(0), _status(0)])

    def sleep(s: float) -> None:
        now[0] += s

    samples, waited = dc.collector_samples(
        lambda: next(reads), window_s=2.0, sleep=sleep, monotonic=lambda: now[0]
    )
    assert [s["queued"] for s in samples] == ["25", "9", "0"]  # stops at the empty queue
    assert waited == pytest.approx(1.0)
    assert dc.check_collector(samples, waited).status == "OK"

    now[0] = 0.0
    stuck = iter([_status(25)] * 10)
    samples, waited = dc.collector_samples(
        lambda: next(stuck), window_s=2.0, sleep=sleep, monotonic=lambda: now[0]
    )
    assert len(samples) == 5  # 0, 0.5, 1.0, 1.5, 2.0 s
    assert waited == pytest.approx(2.0)
    assert dc.check_collector(samples, waited).status == "FAIL"

    now[0] = 0.0
    clean = iter([_status(0)])
    samples, waited = dc.collector_samples(
        lambda: next(clean), window_s=2.0, sleep=sleep, monotonic=lambda: now[0]
    )
    assert len(samples) == 1
    assert waited == 0.0
