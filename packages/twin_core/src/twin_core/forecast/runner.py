"""Forecast use cases on top of the model: forecast with comparison, levers, economic effect.

Pure and synchronous (the API runs them in a worker thread). Inputs are a
:class:`ForecastContext` — configuration, calibration, plant state, targets and month.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from twin_core.config import TwinConfig
from twin_core.forecast.calibration import month_bounds
from twin_core.forecast.effect import EffectResult, economics, effect_result, improvement_overrides
from twin_core.forecast.levers import (
    LeversResult,
    evaluate_levers,
    lever_specs,
    shifts_needed,
)
from twin_core.forecast.model import Paths, Setup, build_setup, simulate
from twin_core.forecast.overrides import Overrides
from twin_core.forecast.params import CalibrationParams, PlantState, Targets
from twin_core.forecast.result import ForecastResult, build_result, outcome


@dataclass(frozen=True)
class ForecastContext:
    cfg: TwinConfig
    params: CalibrationParams
    state: PlantState
    targets: Targets
    month: str
    calibration_id: int | None = None

    @property
    def bounds(self) -> tuple[datetime, datetime]:
        return month_bounds(self.cfg, self.month)


def default_seed(cfg: TwinConfig) -> int:
    seed = cfg.simulation.forecast.seed
    return cfg.simulation.clock.random_seed if seed is None else seed


def run_paths(
    ctx: ForecastContext, overrides: Overrides | None, *, n_runs: int, seed: int
) -> tuple[Setup, Paths]:
    m0, m1 = ctx.bounds
    setup = build_setup(
        ctx.cfg, ctx.params, ctx.state, month_start=m0, month_end=m1, overrides=overrides
    )
    return setup, simulate(setup, n_runs, seed)


def forecast(
    ctx: ForecastContext,
    overrides: Overrides | None = None,
    *,
    n_runs: int,
    seed: int,
    base: Paths | None = None,
    compare: bool = True,
) -> tuple[ForecastResult, Paths, Paths | None]:
    """Forecast for ``overrides``; with non-empty overrides and ``compare`` the baseline (same
    seed, common random numbers) is run too (or ``base`` reused) for the comparison."""
    ov = overrides or Overrides()
    setup, paths = run_paths(ctx, ov, n_runs=n_runs, seed=seed)
    base_paths: Paths | None = None
    if compare and not ov.is_empty():
        base_paths = base if base is not None else run_paths(ctx, None, n_runs=n_runs, seed=seed)[1]
    result = build_result(
        ctx.cfg,
        setup,
        paths,
        month=ctx.month,
        state=ctx.state,
        targets=ctx.targets,
        seed=seed,
        n_runs=n_runs,
        base=base_paths,
        calibration_id=ctx.calibration_id,
        warnings=list(ctx.params.warnings),
    )
    return result, paths, base_paths


def levers(
    ctx: ForecastContext,
    *,
    n_runs: int,
    seed: int,
    assumptions: dict[str, object] | None = None,
) -> LeversResult:
    """All levers of §10.4 plus the extra shifts needed to reach each target."""
    cfg = ctx.cfg
    _, m1 = ctx.bounds
    targets = ctx.targets.as_dict()
    econ, _ = economics(cfg, dict(assumptions or {}))
    base_setup, base = run_paths(ctx, None, n_runs=n_runs, seed=seed)

    def run(ov: Overrides) -> tuple[Paths, tuple[str, ...], int]:
        setup, paths = run_paths(ctx, ov, n_runs=n_runs, seed=seed)
        return paths, setup.areas, setup.extra_shift_count

    specs = lever_specs(cfg, ctx.params, as_of=ctx.state.as_of, month_end=m1)
    rows = evaluate_levers(cfg, specs, base=base, run=run, targets=targets, econ=econ)
    needed = shifts_needed(
        cfg, base=base, run=run, targets=targets, as_of=base_setup.horizon.as_of, month_end=m1
    )
    return LeversResult(
        month=ctx.month,
        as_of=base_setup.horizon.as_of,
        n_runs=n_runs,
        seed=seed,
        base=outcome(base, targets),
        levers=rows,
        shifts_needed_for_target=needed,
        calibration_id=ctx.calibration_id,
    )


def typical_state(cfg: TwinConfig, params: CalibrationParams, month: str) -> PlantState:
    """A typical start of ``month``: initial buffers and kits from the configuration, no open
    repairs, filters at a random point of their life, nothing produced yet."""
    m0, _ = month_bounds(cfg, month)
    return PlantState(
        as_of=m0,
        mtd_output=0,
        buffers={code: float(b.initial) for code, b in params.buffers.items()},
        kits={p: float(k) for p, k in cfg.simulation.ckd_supply.initial_kits.items()},
        source="typical",
    )


def effect(
    cfg: TwinConfig,
    params: CalibrationParams,
    *,
    month: str,
    targets: Targets,
    scenario: Overrides | None,
    assumptions: dict[str, object] | None,
    n_runs: int,
    seed: int,
) -> EffectResult:
    """§10.5 over a full typical month (so the year is ×12); default scenario «с системой»."""
    econ, listed = economics(cfg, dict(assumptions or {}))
    ov = scenario if scenario is not None else improvement_overrides(cfg, params)
    ctx = ForecastContext(cfg, params, typical_state(cfg, params, month), targets, month)
    _, base = run_paths(ctx, None, n_runs=n_runs, seed=seed)
    setup, paths = run_paths(ctx, ov, n_runs=n_runs, seed=seed)
    return effect_result(
        cfg,
        base,
        paths,
        month=month,
        areas=setup.areas,
        econ=econ,
        assumptions=listed,
        extra_shifts=setup.extra_shift_count,
        overrides=ov,
        n_runs=n_runs,
        seed=seed,
    )
