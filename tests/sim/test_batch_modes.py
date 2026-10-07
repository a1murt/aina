"""backfill and ml-dataset modes (SPEC §6.1), CLI entry points."""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest

from qost_sim.__main__ import main as sim_main
from qost_sim.backfill import run_backfill
from qost_sim.ml_dataset import add_months, generate
from support import mutate
from twin_core.config import TwinConfig
from twin_core.event_sink import MemorySink
from twin_core.events import parse_event


async def test_backfill_writes_validated_events_in_batches(cfg: TwinConfig) -> None:
    demo = cfg.simulation.clock.demo_start
    sink = MemorySink()
    stats = await run_backfill(cfg, sink, start=demo - timedelta(days=2), end=demo, batch_size=500)
    assert stats.events == len(sink.events) > 10_000
    assert sink.batches == stats.batches
    kinds = Counter(e.kind for e in sink.events)
    assert set(kinds) == {"state", "unit", "defect", "telemetry", "alarm", "buffer_level", "ckd"}
    assert all(e.source == "sim" and e.ts < demo for e in sink.events)
    assert [e.ts for e in sink.events] == sorted(e.ts for e in sink.events)
    ids = [e.event_id for e in sink.events]
    assert len(set(ids)) == len(ids)
    again = MemorySink()
    await run_backfill(cfg, again, start=demo - timedelta(days=2), end=demo)
    assert [e.event_id for e in again.events] == ids  # idempotent re-run (same ids)
    period = cfg.simulation.telemetry.backfill_sample_period_s
    tele = sorted({e.ts for e in sink.events if e.kind == "telemetry"})
    assert (tele[1] - tele[0]).total_seconds() == period
    with pytest.raises(ValueError, match="not after"):
        await run_backfill(cfg, sink, start=demo, end=demo)


def test_backfill_cli_writes_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "history.jsonl"
    code = sim_main(
        [
            "backfill",
            "--sink",
            f"jsonl:{out}",
            "--from",
            "2026-10-15T07:00:00+05:00",
            "--to",
            "2026-10-15T15:00:00+05:00",
        ]
    )
    assert code == 0
    lines = out.read_text("utf-8").splitlines()
    assert lines
    assert parse_event(lines[0]).source == "sim"
    assert "events in" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="time zone"):
        sim_main(["backfill", "--sink", "null:", "--from", "2026-10-15T07:00:00"])


def test_ml_dataset_writes_parquet(cfg: TwinConfig, tmp_path: Path) -> None:
    meta = generate(cfg, tmp_path, months=1)
    spec = cfg.simulation.ml_dataset
    assert meta["seed"] == spec.random_seed
    assert json.loads((tmp_path / "meta.json").read_text("utf-8"))["rows"] == meta["rows"]
    tele = pl.read_parquet(tmp_path / "telemetry" / "*.parquet")
    assert tele.columns == ["ts", "equipment", "type", "signal", "value"]
    assert len(tele) == meta["rows"]["telemetry"]
    assert "degradation" not in set(tele["signal"].cast(pl.String))
    step = tele.filter(pl.col("equipment") == "CONV-03", pl.col("signal") == "vibration_mm_s")["ts"]
    assert (step[1] - step[0]).total_seconds() == spec.telemetry_period_s
    downtime = pl.read_parquet(tmp_path / "downtime.parquet")
    wear = downtime.filter(pl.col("wear"))
    assert set(wear["reason"]) <= {"RB-TOOL", "EL-SENSOR", "ME-CHAIN", "ME-JAM", "EL-DRIVE"}
    assert downtime.filter(pl.col("microstop"))["duration_s"].max() < 300  # type: ignore[operator]
    assert downtime.filter(pl.col("planned"))["state"].unique().to_list() == ["DOWN_PLANNED"]
    oracle = pl.read_parquet(tmp_path / "oracle_degradation.parquet")
    assert oracle["degradation"].max() <= 1.0  # type: ignore[operator]
    units = pl.read_parquet(tmp_path / "units.parquet")
    assert set(units["line"]) == set(cfg.flow_lines)
    states = pl.read_parquet(tmp_path / "states.parquet")
    assert (states["end"] >= states["start"]).all()
    assert add_months(spec.from_, 12) == spec.from_.replace(year=spec.from_.year + 1)


def test_calibrate_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert sim_main(["calibrate", "--days", "5", "--warmup", "1"]) in (0, 1)
    assert "throughput per shift" in capsys.readouterr().out


def test_cli_refuses_a_broken_signal_model(
    config_copy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mutate(config_copy / "simulation.yaml", '"140 + 8*precursor(2)', '"140 + 8*precursr(2)')
    monkeypatch.setenv("PLANT_CONFIG_DIR", str(config_copy))
    with pytest.raises(SystemExit) as caught:
        sim_main(["calibrate"])
    assert caught.value.code == 2
    assert "precursr" in capsys.readouterr().err
