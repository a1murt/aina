"""FR-SIM-01: same seed + same interventions => bit-identical results."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import timedelta

from qost_sim.live import warm_model
from qost_sim.model import Inject, PlantModel
from qost_sim.model.records import ORACLE
from sim_support import key, run, to_jsonl
from support import REPO_ROOT
from twin_core.config import TwinConfig
from twin_core.config.simulation import DefectMultiplierInject, FailureInject


def scenario(cfg: TwinConfig) -> tuple[dict[str, object], list[tuple[object, Inject]]]:
    demo = cfg.simulation.clock.demo_start
    window: dict[str, object] = {
        "start": demo - timedelta(days=2),
        "until": demo + timedelta(hours=20),
    }
    injects: list[tuple[object, Inject]] = [
        (demo + timedelta(hours=1), cfg.scenarios["S1-CHAIN-BREAK"].inject),
        (
            demo + timedelta(hours=2),
            DefectMultiplierInject(
                type="defect_multiplier", areas=["PAINT"], factor=2.0, duration_min=120
            ),
        ),
        (
            demo + timedelta(hours=3),
            FailureInject(type="failure", equipment="ABB-01", reason="RB-TOOL", duration_min=30),
        ),
    ]
    return window, injects


def test_same_seed_and_interventions_give_identical_bytes(cfg: TwinConfig) -> None:
    window, injects = scenario(cfg)
    first = to_jsonl(*run(cfg, injects=injects, telemetry_period_s=300, **window))  # type: ignore[arg-type]
    second = to_jsonl(*run(cfg, injects=injects, telemetry_period_s=300, **window))  # type: ignore[arg-type]
    assert first == second
    assert first.count("\n") > 5000
    other_seed = to_jsonl(
        *run(cfg, seed=1, injects=injects, telemetry_period_s=300, **window)  # type: ignore[arg-type]
    )
    assert other_seed != first
    no_injects = to_jsonl(*run(cfg, telemetry_period_s=300, **window))  # type: ignore[arg-type]
    assert no_injects != first


def test_chunked_runs_equal_one_run(cfg: TwinConfig) -> None:
    """Live pacing advances the model in small steps; the result must not depend on them."""
    window, injects = scenario(cfg)
    _, whole = run(cfg, injects=injects, **window)  # type: ignore[arg-type]
    steps = [37.0, 1.0, 0.25, 600.0, 3.5] * 400 + [3600.0] * 200
    _, chunked = run(cfg, injects=injects, chunks=steps, **window)  # type: ignore[arg-type]
    assert [key(r) for r in chunked] == [key(r) for r in whole]


def test_telemetry_does_not_change_the_plant(cfg: TwinConfig) -> None:
    window, injects = scenario(cfg)

    def plant(period: float | None) -> list[tuple[object, ...]]:
        _, records = run(cfg, injects=injects, telemetry_period_s=period, **window)  # type: ignore[arg-type]
        return [key(r) for r in records if r.kind not in ("telemetry", ORACLE)]

    base = plant(None)
    assert plant(60) == base
    assert plant(300) == base


def test_live_warm_up_equals_the_backfill_history(cfg: TwinConfig) -> None:
    """The live model at demo_start continues exactly where the backfill history stopped."""
    demo = cfg.simulation.clock.demo_start
    start = demo - timedelta(days=5)
    later = demo + timedelta(hours=10)
    # backfill-like: telemetry every 300 s, hourly chunks straight through demo_start, with the
    # same at_min scenario schedule as the live run
    backfill = PlantModel(cfg, start=start, telemetry_period_s=300)
    for s in cfg.simulation.scenarios:
        if s.at_min is not None:
            backfill.schedule(backfill.sec(demo) + s.at_min * 60, s.inject, scenario_id=s.id)
    records = []
    t = 0.0
    while t < backfill.sec(later):
        t = min(t + 3600.0, backfill.sec(later))
        backfill.run_until(t)
        records.extend(backfill.drain())
    # live: silent warm-up to demo_start, then telemetry every 60 s
    live = warm_model(cfg, start=start, demo_start=demo, telemetry_period_s=60)
    live.run_until_time(later)
    live_records = live.drain()
    demo_s = backfill.sec(demo)
    expected = [key(r) for r in records if r.t >= demo_s and r.kind not in ("telemetry", ORACLE)]
    got = [key(r) for r in live_records if r.kind not in ("telemetry", ORACLE)]
    assert len(got) > 1000
    assert got == expected
    assert backfill.snapshot()["kits"] == live.snapshot()["kits"]


SCRIPT = """
import hashlib, sys
from datetime import timedelta
sys.path.insert(0, "tests")
from sim_support import run, to_jsonl
from twin_core.config import load_config
cfg = load_config("config", tag_map=False)
demo = cfg.simulation.clock.demo_start
model, records = run(cfg, start=demo - timedelta(days=1), until=demo + timedelta(hours=12),
                     telemetry_period_s=300)
print(hashlib.sha256(to_jsonl(model, records).encode()).hexdigest())
"""


def test_results_do_not_depend_on_hash_randomization(cfg: TwinConfig) -> None:
    demo = cfg.simulation.clock.demo_start
    model, records = run(
        cfg,
        start=demo - timedelta(days=1),
        until=demo + timedelta(hours=12),
        telemetry_period_s=300,
    )
    expected = hashlib.sha256(to_jsonl(model, records).encode()).hexdigest()
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == expected
