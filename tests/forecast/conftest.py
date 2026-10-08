"""Fixtures of the forecast tests: one virtual-plant history up to ``demo_start`` (≈ 0.4 s)."""

from __future__ import annotations

import pytest

from forecast_support import History, demo_history
from twin_core.forecast.calibration import targets_from_config
from twin_core.forecast.params import CalibrationParams, PlantState
from twin_core.forecast.runner import ForecastContext


@pytest.fixture(scope="session")
def history() -> History:
    return demo_history()


@pytest.fixture(scope="session")
def params(history: History) -> CalibrationParams:
    return history.params()


@pytest.fixture(scope="session")
def state(history: History) -> PlantState:
    return history.state()


@pytest.fixture(scope="session")
def ctx(history: History, params: CalibrationParams, state: PlantState) -> ForecastContext:
    cfg = history.cfg
    return ForecastContext(cfg, params, state, targets_from_config(cfg, "2026-10"), "2026-10")
