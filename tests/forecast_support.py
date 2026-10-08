"""Helpers for forecast tests: virtual-plant history and an in-memory forecast backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import cache

from qost_api.auth import Principal
from qost_api.forecast.data import RunRecord, StoredRun, StoredSnapshot
from qost_sim.forecast_inputs import calibration_inputs, plant_state
from qost_sim.model import PlantModel, Rec
from support import CONFIG_DIR
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig, load_config
from twin_core.forecast.calibration import (
    CalibrationInputs,
    calibrate,
    calibration_window,
    targets_from_config,
)
from twin_core.forecast.params import CalibrationParams, PlantState, Targets


@dataclass(frozen=True)
class History:
    """A virtual-plant run from ``clock.backfill_from`` to ``until`` and its records."""

    cfg: TwinConfig
    model: PlantModel
    records: tuple[Rec, ...]

    @property
    def now(self) -> datetime:
        return self.model.now

    def inputs(self) -> CalibrationInputs:
        return calibration_inputs(self.model, self.records)

    def params(self) -> CalibrationParams:
        return calibrate(self.cfg, self.inputs(), computed_at=self.now)

    def state(self) -> PlantState:
        return plant_state(self.model, self.records)


def run_history(cfg: TwinConfig, until: datetime, seed: int | None = None) -> History:
    model = PlantModel(cfg, start=cfg.simulation.clock.backfill_from, seed=seed)
    model.run_until_time(until)
    return History(cfg, model, tuple(model.drain()))


@cache
def demo_history(seed: int | None = None) -> History:
    """History up to ``clock.demo_start`` (cached per process; do not mutate the model)."""
    cfg = load_config(CONFIG_DIR, tag_map=False)
    return run_history(cfg, cfg.simulation.clock.demo_start, seed)


@dataclass
class MemoryBackend:
    """:class:`qost_api.forecast.data.ForecastBackend` over fixed inputs (no database)."""

    inputs: CalibrationInputs
    state: PlantState
    targets_: Targets | None = None
    snapshots: dict[int, StoredSnapshot] = field(default_factory=dict)
    runs: dict[int, StoredRun] = field(default_factory=dict)
    input_calls: int = 0

    async def calibration_inputs(
        self,
        cfg: TwinConfig,
        *,
        window_from: datetime,
        window_to: datetime,
        working_days: int,
        as_of: datetime,
    ) -> CalibrationInputs:
        self.input_calls += 1
        return self.inputs

    async def plant_state(self, cfg: TwinConfig, *, as_of: datetime) -> PlantState:
        return self.state

    async def targets(self, cfg: TwinConfig, month: str) -> Targets:
        return self.targets_ or targets_from_config(cfg, month)

    async def find_snapshot(
        self, *, window_to: datetime, window_days: int, config_hash: str
    ) -> StoredSnapshot | None:
        for snap in self.snapshots.values():
            p = snap.params
            if (
                ensure_utc(p.window_to) == ensure_utc(window_to)
                and p.working_days == window_days
                and p.config_hash == config_hash
            ):
                return snap
        return None

    async def save_snapshot(
        self, params: CalibrationParams, *, ts: datetime, principal: Principal
    ) -> StoredSnapshot:
        snap = StoredSnapshot(len(self.snapshots) + 1, ts, params.working_days, params)
        self.snapshots[snap.id] = snap
        return snap

    async def save_run(self, record: RunRecord, *, principal: Principal) -> int:
        run_id = len(self.runs) + 1
        self.runs[run_id] = StoredRun(run_id, principal.user_id, record)
        return run_id

    async def get_run(self, run_id: int) -> StoredRun | None:
        return self.runs.get(run_id)


def memory_backend(history: History) -> MemoryBackend:
    cfg = history.cfg
    w0, w1, days = calibration_window(cfg, history.now)
    inputs = history.inputs()
    assert inputs.window_from == w0
    assert inputs.window_to == w1
    assert inputs.working_days == days
    return MemoryBackend(inputs=inputs, state=history.state())
