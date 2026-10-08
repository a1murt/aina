"""Engine core over the virtual plant: 14 calendar days of simulated history (replay mode)."""

from __future__ import annotations

import itertools
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pytest

from qost_engine.core import BottleneckRow, DqUpsert, EngineCore, KpiShiftRow, StateInterval
from qost_engine.writer import coalesce
from qost_sim.model import EventFactory, PlantModel
from twin_core.config import TwinConfig
from twin_core.events import FIRST_EXIT_RESULTS, UnitEvent

DAYS = 14


@pytest.fixture(scope="module")
def replay(cfg: TwinConfig) -> dict[str, object]:
    start = cfg.simulation.clock.backfill_from
    end = start + timedelta(days=DAYS)
    model = PlantModel(cfg, start=start)
    factory = EventFactory.deterministic(site="KST", t0=model.t0, seed=model.seed, nonce="t")
    model.run_until_time(end)
    events = [e for e in factory.convert_all(model.drain()) if e.kind != "telemetry"]
    began = time.perf_counter()
    core = EngineCore(cfg, mode="replay")
    effects = []
    for event in events:
        core.apply(event)
        effects.extend(core.drain()[0])
    core.advance_to(end)
    effects.extend(core.drain()[0])
    return {
        "events": events,
        "effects": effects,
        "core": core,
        "seconds": time.perf_counter() - began,
        "end": end,
    }


def test_runtime_and_ordering(replay: dict[str, object]) -> None:
    core = replay["core"]
    assert isinstance(core, EngineCore)
    assert core.stats["late_events"] == 0
    assert core.stats["dropped_units"] == 0
    seconds = replay["seconds"]
    assert isinstance(seconds, float)
    assert seconds < 3.0


def test_intervals_are_contiguous_and_non_overlapping(replay: dict[str, object]) -> None:
    rows = [e for e in coalesce(replay["effects"]) if isinstance(e, StateInterval)]  # type: ignore[arg-type]
    by_entity: dict[str, list[StateInterval]] = defaultdict(list)
    for r in rows:
        by_entity[r.entity].append(r)
    assert len(by_entity) == 18
    for entity, items in by_entity.items():
        items.sort(key=lambda r: r.start)
        for prev, nxt in itertools.pairwise(items):
            assert prev.end == nxt.start, entity
            assert prev.state != nxt.state or prev.reason_code != nxt.reason_code, entity
        assert items[-1].end is None


def test_time_model_and_counts_per_shift(cfg: TwinConfig, replay: dict[str, object]) -> None:
    kpis = [e for e in replay["effects"] if isinstance(e, KpiShiftRow)]  # type: ignore[attr-defined]
    assert len(kpis) == 4 * 2 * 10  # 10 working days x 2 shifts x 4 lines
    counted: Counter[tuple[str, str, str]] = Counter()
    for e in replay["events"]:  # type: ignore[attr-defined]
        if isinstance(e, UnitEvent) and e.data.result in FIRST_EXIT_RESULTS:
            shift = cfg.calendar.shift_at(e.ts, working_only=True)
            assert shift is not None
            counted[(e.data.line, shift.shift_date.isoformat(), shift.code)] += 1
    for k in kpis:
        v = k.values
        assert v["pot"] == pytest.approx(480)
        assert v["pdot"] + v["adot"] + v["adet"] + v["aust"] + v["apt"] == pytest.approx(480)
        assert v["pq"] == counted[(k.line, k.shift_date.isoformat(), k.shift_code)]
        assert 0.5 < v["oee"] < 1.0
        assert v["effectiveness"] <= 1.0


def test_flow_balance_and_bottleneck(replay: dict[str, object]) -> None:
    dq = [e for e in replay["effects"] if isinstance(e, DqUpsert)]  # type: ignore[attr-defined]
    assert not [d for d in dq if d.rule_id == "DQ-04"]
    assert all(d.details["direction"] == "log_exceeds_loss" for d in dq if d.rule_id == "DQ-02")
    shares: dict[str, float] = defaultdict(float)
    for b in replay["effects"]:  # type: ignore[attr-defined]
        if isinstance(b, BottleneckRow):
            shares[b.line] += b.sole_share + b.shifting_share
    assert max(shares, key=shares.__getitem__) == "PAINT-1"


def test_snapshot_is_json_and_restorable(cfg: TwinConfig, replay: dict[str, object]) -> None:
    import json

    core = replay["core"]
    assert isinstance(core, EngineCore)
    raw = json.dumps(core.snapshot())
    restored = EngineCore.restore(cfg, json.loads(raw), mode="replay")
    assert restored.snapshot() == core.snapshot()
    assert len(raw) < 200_000
    assert isinstance(replay["end"], datetime)
