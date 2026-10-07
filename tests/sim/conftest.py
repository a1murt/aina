"""Shared runs of the virtual plant (computed once per test session)."""

from __future__ import annotations

import pytest

from qost_sim.calibration import run_calibration
from sim_support import CalibrationRun
from twin_core.config import TwinConfig


@pytest.fixture(scope="session")
def calibration_run(cfg: TwinConfig) -> CalibrationRun:
    """FR-SIM-03 run: 5 working days of warm-up + 20 measured, seed = clock.random_seed."""
    return run_calibration(cfg, warmup_days=5, days=20)
