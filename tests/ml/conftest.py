"""Small PdM data for the fast T-ML tests: one simulated month (seed 7), built once per session."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from qost_ml.dataset import RawDataset, build_tables, load_raw
from qost_ml.spec import PdmSpec
from qost_ml.train import SplitBounds, TrainResult, train_type
from qost_sim.ml_dataset import generate
from twin_core.config import TwinConfig

SMALL_UNITS = ("ABB-01", "ABB-02", "ABB-03", "ABB-04", "CONV-03", "JIG-01", "OVEN-01")


@pytest.fixture(scope="session")
def spec(cfg: TwinConfig) -> PdmSpec:
    return PdmSpec.from_config(cfg)


@pytest.fixture(scope="session")
def ml_raw_dir(cfg: TwinConfig, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("ml_raw")
    generate(cfg, out, months=1)
    return out


@pytest.fixture(scope="session")
def raw(ml_raw_dir: Path, spec: PdmSpec) -> RawDataset:
    return load_raw(ml_raw_dir, spec)


@pytest.fixture(scope="session")
def small_tables(raw: RawDataset, spec: PdmSpec, cfg: TwinConfig) -> dict[str, pl.DataFrame]:
    return build_tables(raw, spec, cfg.calendar, units=SMALL_UNITS)


@pytest.fixture(scope="session")
def small_bounds(cfg: TwinConfig, spec: PdmSpec) -> SplitBounds:
    """The month split by days (train 16, val 7, test 8) instead of 9/1/2 months."""
    full = SplitBounds.from_config(cfg, spec)
    day_us = 24 * 3600 * 1_000_000
    return SplitBounds(
        start_us=full.start_us,
        train_end_us=full.start_us + 16 * day_us,
        val_end_us=full.start_us + 23 * day_us,
        test_end_us=full.start_us + 31 * day_us,
        horizon_us=full.horizon_us,
        timezone=full.timezone,
    )


@pytest.fixture(scope="session")
def robot_model(
    small_tables: dict[str, pl.DataFrame], spec: PdmSpec, small_bounds: SplitBounds
) -> TrainResult:
    return train_type(
        small_tables["robot"],
        spec=spec,
        bounds=small_bounds,
        equipment_type="robot",
        num_boost_round=80,
    )
