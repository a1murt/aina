"""The fast model against the SimPy virtual plant (analogue of the SPEC §10.3 AC, T-FC).

``validation`` (``make test`` / ``make forecast-validate``, not ``make check``): 60 seeds, two
check points; mean bias within 3 % of the remaining output and P10–P90 coverage 65–95 %.
``make check`` keeps one deterministic seed (±5 %).
"""

from __future__ import annotations

import pytest

from qost_sim.forecast_validation import default_check_points, summarize, validate
from twin_core.config import TwinConfig


def test_one_seed_from_demo_start(cfg: TwinConfig) -> None:
    demo = cfg.simulation.clock.demo_start
    rows = validate(cfg, seeds=[cfg.simulation.clock.random_seed], check_points=[demo], n_runs=500)
    (row,) = rows
    assert abs(row.mean - row.actual) <= 0.05 * row.remaining_actual
    assert row.p10 <= row.p50 <= row.p90


@pytest.mark.validation
def test_bias_and_coverage_over_seeds(cfg: TwinConfig) -> None:
    rows = validate(
        cfg, seeds=list(range(1, 61)), check_points=default_check_points(cfg), n_runs=2000
    )
    for s in summarize(rows):
        print(
            f"\n{s.as_of:%Y-%m-%d %H:%M}Z: bias {s.bias_pct:+.2f} %, P50 {s.p50_bias_pct:+.2f} %, "
            f"coverage {s.coverage:.0%}"
        )
        assert abs(s.bias_pct) <= 3.0
        assert 0.65 <= s.coverage <= 0.95
