"""Validation of the fast forecast model against the virtual plant (SPEC §10.3 AC analogue).

For each seed the SimPy plant runs from ``clock.backfill_from``; at every check point the fast
model is calibrated on the plant's own history (last ``window_days`` working days) and started
from its exact state; then the plant runs on to the end of the month and its actual finished
output is compared with the forecast. Reported: bias of the forecast mean as a share of the
**remaining** output, coverage of the P10–P90 interval, timing.

``python -m qost_sim.forecast_validation --seeds 60`` (``make forecast-validate``) prints the
table recorded in ``docs/PROGRESS.md``; ``tests/forecast/test_validation_sim.py`` checks it.
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass
from datetime import datetime

from qost_sim.forecast_inputs import calibration_inputs, finished_output, plant_state
from qost_sim.model import PlantModel, Rec
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig, load_config
from twin_core.forecast.calibration import (
    calibrate,
    month_bounds,
    month_of,
    targets_from_config,
)
from twin_core.forecast.runner import ForecastContext, forecast


@dataclass(frozen=True, slots=True)
class ValidationRow:
    seed: int
    as_of: datetime
    mtd: int
    actual: int
    mean: float
    p10: float
    p50: float
    p90: float
    seconds: float

    @property
    def remaining_actual(self) -> int:
        return self.actual - self.mtd

    @property
    def covered(self) -> bool:
        return self.p10 <= self.actual <= self.p90


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    as_of: datetime
    seeds: int
    actual_mean: float
    forecast_mean: float
    forecast_p50: float
    remaining_mean: float
    bias_pct: float
    """(mean forecast - mean actual) / mean remaining actual output, %."""
    p50_bias_pct: float
    coverage: float
    """Share of seeds whose actual total lies within P10–P90."""
    mean_seconds: float


def validate(
    cfg: TwinConfig,
    *,
    seeds: list[int],
    check_points: list[datetime],
    n_runs: int = 5000,
    forecast_seed: int = 1,
) -> list[ValidationRow]:
    points = sorted(ensure_utc(p) for p in check_points)
    month = month_of(cfg, points[0])
    _, month_end = month_bounds(cfg, month)
    targets = targets_from_config(cfg, month)
    rows: list[ValidationRow] = []
    for seed in seeds:
        model = PlantModel(cfg, start=cfg.simulation.clock.backfill_from, seed=seed)
        records: list[Rec] = []
        pending: list[tuple[datetime, int, float, float, float, float, float]] = []
        for point in points:
            model.run_until_time(point)
            records.extend(model.drain())
            started = time.perf_counter()
            params = calibrate(cfg, calibration_inputs(model, records), computed_at=point)
            state = plant_state(model, records)
            ctx = ForecastContext(cfg, params, state, targets, month)
            result, _, _ = forecast(ctx, n_runs=n_runs, seed=forecast_seed)
            seconds = time.perf_counter() - started
            s = result.summary
            pending.append((point, state.mtd_output, s.mean, s.p10, s.p50, s.p90, seconds))
        model.run_until_time(month_end)
        records.extend(model.drain())
        month_start, _ = month_bounds(cfg, month)
        actual = finished_output(cfg, model, records, month_start, month_end)
        for point, mtd, mean, p10, p50, p90, seconds in pending:
            rows.append(ValidationRow(seed, point, mtd, actual, mean, p10, p50, p90, seconds))
    return rows


def summarize(rows: list[ValidationRow]) -> list[ValidationSummary]:
    out: list[ValidationSummary] = []
    for point in sorted({r.as_of for r in rows}):
        group = [r for r in rows if r.as_of == point]
        actual = statistics.fmean(r.actual for r in group)
        mean = statistics.fmean(r.mean for r in group)
        p50 = statistics.fmean(r.p50 for r in group)
        remaining = statistics.fmean(r.remaining_actual for r in group)
        out.append(
            ValidationSummary(
                as_of=point,
                seeds=len(group),
                actual_mean=actual,
                forecast_mean=mean,
                forecast_p50=p50,
                remaining_mean=remaining,
                bias_pct=100.0 * (mean - actual) / remaining if remaining else 0.0,
                p50_bias_pct=100.0 * (p50 - actual) / remaining if remaining else 0.0,
                coverage=sum(r.covered for r in group) / len(group),
                mean_seconds=statistics.fmean(r.seconds for r in group),
            )
        )
    return out


def default_check_points(cfg: TwinConfig) -> list[datetime]:
    """Start of the demo month and the demo start itself."""
    demo = cfg.simulation.clock.demo_start
    month_start, _ = month_bounds(cfg, month_of(cfg, demo))
    return [month_start, demo]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=60)
    parser.add_argument("--first-seed", type=int, default=1)
    parser.add_argument("--runs", type=int, default=5000)
    args = parser.parse_args(argv)
    cfg = load_config()
    seeds = list(range(args.first_seed, args.first_seed + args.seeds))
    rows = validate(cfg, seeds=seeds, check_points=default_check_points(cfg), n_runs=args.runs)
    tz = cfg.timezone
    print(
        f"{'as of':<17} {'seeds':>5} {'actual':>8} {'fc mean':>8} {'fc P50':>8} "
        f"{'remain':>7} {'bias %':>7} {'P50 b%':>7} {'P10-P90':>8} {'sec':>5}"
    )
    for s in summarize(rows):
        print(
            f"{s.as_of.astimezone(tz):%Y-%m-%d %H:%M} {s.seeds:>5} {s.actual_mean:>8.1f} "
            f"{s.forecast_mean:>8.1f} {s.forecast_p50:>8.1f} {s.remaining_mean:>7.1f} "
            f"{s.bias_pct:>7.2f} {s.p50_bias_pct:>7.2f} {s.coverage:>8.0%} {s.mean_seconds:>5.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
