"""Calibration (SPEC §10.1): estimators, Bayesian fallbacks, window, recovery from the sim."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from forecast_support import History
from twin_core.config import TwinConfig
from twin_core.forecast.calibration import (
    CalibrationInputs,
    LineShiftFacts,
    Stop,
    calibrate,
    calibration_window,
    month_bounds,
    targets_from_config,
)
from twin_core.forecast.params import CalibrationParams

W0 = datetime(2026, 9, 17, 19, 0, tzinfo=UTC)
W1 = datetime(2026, 10, 15, 19, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 08:00 local, inside shift A


def _stop(
    eq: str, minute: int, duration: float, *, planned: bool = False, reason: str = "EL-SENSOR"
) -> Stop:
    start = T0 + timedelta(minutes=minute)
    return Stop(eq, start, start + timedelta(minutes=duration), planned, reason)


def _inputs(
    *,
    stops: list[Stop] | None = None,
    hours: float = 300.0,
    shifts: list[LineShiftFacts] | None = None,
    codes: dict[str, dict[str, int]] | None = None,
) -> CalibrationInputs:
    return CalibrationInputs(
        window_from=W0,
        window_to=W1,
        working_days=20,
        operating_h={"ABB-01": hours, "CONV-03": hours, "OVEN-01": hours, "BOOTH-01": hours},
        stops=tuple(stops or []),
        line_shifts=tuple(shifts or []),
        defect_codes=codes or {},
    )


def _calibrate(cfg: TwinConfig, inputs: CalibrationInputs) -> CalibrationParams:
    return calibrate(cfg, inputs, computed_at=W1)


def test_window_is_last_complete_working_days(cfg: TwinConfig) -> None:
    w0, w1, days = calibration_window(cfg, cfg.simulation.clock.demo_start)
    tz = cfg.timezone
    assert days == 20
    assert w1.astimezone(tz) == datetime(2026, 10, 16, tzinfo=tz)
    assert w0.astimezone(tz) == datetime(2026, 9, 18, tzinfo=tz)
    assert cfg.calendar.is_working_day(w0.astimezone(tz).date())


def test_month_bounds_and_targets(cfg: TwinConfig) -> None:
    m0, m1 = month_bounds(cfg, "2026-12")
    tz = cfg.timezone
    assert m0.astimezone(tz).date() == date(2026, 12, 1)
    assert m1.astimezone(tz).date() == date(2027, 1, 1)
    t = targets_from_config(cfg, "2026-10")
    assert (t.plant_target, t.line_plan, t.source) == (5500, 4800, "config")
    empty = targets_from_config(cfg, "2027-01")
    assert empty.plant_target is None
    assert empty.line_plan is None


def test_failure_rate_data_when_enough_failures(cfg: TwinConfig) -> None:
    stops = [_stop("ABB-01", 60 * i, 20) for i in range(5)]
    eq = _calibrate(cfg, _inputs(stops=stops)).equipment["ABB-01"]
    assert eq.failures.source == "data"
    assert eq.failures.n == 5
    assert eq.failures.per_h == pytest.approx(5 / 300)
    assert (eq.failures.shape, eq.failures.rate_h) == (5.0, 300.0)


@pytest.mark.parametrize("n", [0, 2])
def test_failure_rate_bayesian_fallback_below_three(cfg: TwinConfig, n: int) -> None:
    stops = [_stop("ABB-01", 60 * i, 20) for i in range(n)]
    eq = _calibrate(cfg, _inputs(stops=stops)).equipment["ABB-01"]
    lam0 = 1 / cfg.simulation.failures["robot"].mtbf_h
    a0 = cfg.simulation.forecast.priors.prior_strength
    assert eq.failures.source == "prior"
    assert eq.failures.shape == pytest.approx(a0 + n)
    assert eq.failures.rate_h == pytest.approx(a0 / lam0 + 300)
    assert eq.failures.per_h == pytest.approx((a0 + n) / (a0 / lam0 + 300))


def test_prior_rate_includes_chain_break(cfg: TwinConfig) -> None:
    eq = _calibrate(cfg, _inputs(hours=0.0)).equipment["CONV-03"]
    model = cfg.simulation.failures["conveyor"]
    assert model.chain_break is not None
    lam0 = 1 / model.mtbf_h + 1 / model.chain_break.mtbf_h
    assert eq.failures.per_h == pytest.approx(lam0)


def test_planned_microstop_and_filter_stops_are_not_failures(cfg: TwinConfig) -> None:
    pf = cfg.simulation.paint_filters
    assert pf is not None
    stops = [
        _stop("ABB-01", 0, 30, planned=True, reason="PM-SCHEDULED"),
        _stop("ABB-01", 100, 2, reason="MT-CONSUMABLE"),  # microstop (< 5 min)
        _stop("BOOTH-01", 0, 40, reason=pf.replacement.reason),
    ]
    p = _calibrate(cfg, _inputs(stops=stops))
    assert p.equipment["ABB-01"].failures.n == 0
    assert p.equipment["BOOTH-01"].failures.n == 0


def test_repair_fallback_chain(cfg: TwinConfig) -> None:
    own = [_stop("ABB-01", 100 * i, d) for i, d in enumerate([10, 20, 40])]
    p = _calibrate(cfg, _inputs(stops=own))
    rep = p.equipment["ABB-01"].repair
    assert (rep.basis, rep.source, rep.n) == ("equipment", "data", 3)
    assert rep.mu == pytest.approx(math.log(20))
    # ABB-02 has no own repairs: pooled over robots
    pooled = p.equipment["ABB-02"].repair
    assert (pooled.basis, pooled.n) == ("type", 3)
    # oven: nothing at all -> prior
    oven = p.equipment["OVEN-01"].repair
    assert (oven.basis, oven.source) == ("prior", "prior")
    assert oven.median_min == pytest.approx(cfg.simulation.failures["oven"].mttr.median)


def test_conveyor_prior_repair_mixes_chain_break(cfg: TwinConfig) -> None:
    rep = _calibrate(cfg, _inputs()).equipment["CONV-03"].repair
    model = cfg.simulation.failures["conveyor"]
    assert model.chain_break is not None
    assert model.mttr.median < rep.median_min < model.chain_break.mttr.median


def _shift(
    line: str,
    i: int,
    *,
    apt_min: float,
    exits: int,
    degraded_min: float = 0.0,
    defects: int = 0,
    repaint: int = 0,
) -> LineShiftFacts:
    return LineShiftFacts(
        line=line,
        shift_date=date(2026, 9, 21) + timedelta(days=i),
        shift_code="A",
        apt_s=apt_min * 60,
        degraded_s=degraded_min * 60,
        exits={"ONIX": exits},
        defects=defects,
        repaint_defects=repaint,
    )


def test_efficiency_method_of_moments_and_net_basis(cfg: TwinConfig) -> None:
    ict = cfg.lines["WELD-1"].ict_seconds
    # ~110 bodies of 233 s in 480 min, half an hour degraded (B robots at 50 %)
    shifts = [
        _shift("WELD-1", i, apt_min=480, exits=110 + (i % 3), degraded_min=30) for i in range(6)
    ]
    p = _calibrate(cfg, _inputs(shifts=shifts))
    eff = p.lines["WELD-1"].efficiency
    values = [(110 + (i % 3)) * ict / (480 * 60 - 30 * 60 * 0.5) for i in range(6)]
    mean = sum(values) / 6
    assert eff.source == "data"
    assert eff.n == 6
    assert eff.mean == pytest.approx(mean, rel=1e-9)


def test_efficiency_constant_values_clamp_concentration(cfg: TwinConfig) -> None:
    shifts = [_shift("ASSY-1", i, apt_min=480, exits=110) for i in range(6)]
    eff = _calibrate(cfg, _inputs(shifts=shifts)).lines["ASSY-1"].efficiency
    pr = cfg.simulation.forecast.priors
    assert eff.alpha + eff.beta == pytest.approx(pr.eff_concentration_max)


def test_efficiency_prior_with_few_shifts(cfg: TwinConfig) -> None:
    shifts = [_shift("QC-1", i, apt_min=480, exits=110) for i in range(2)]
    shifts.append(_shift("QC-1", 5, apt_min=30, exits=5))  # too short to count
    eff = _calibrate(cfg, _inputs(shifts=shifts)).lines["QC-1"].efficiency
    assert eff.source == "prior"
    assert eff.n == 2
    assert eff.alpha + eff.beta == pytest.approx(cfg.simulation.forecast.priors.eff_concentration)
    assert 0.9 < eff.mean < 1.0


def test_repaint_passes_count_as_paint_work(cfg: TwinConfig) -> None:
    base = [_shift("PAINT-1", i, apt_min=480, exits=110) for i in range(6)]
    with_repaints = [
        _shift("PAINT-1", i, apt_min=480, exits=110, defects=5, repaint=2) for i in range(6)
    ]
    e0 = _calibrate(cfg, _inputs(shifts=base)).lines["PAINT-1"].efficiency.mean
    e1 = _calibrate(cfg, _inputs(shifts=with_repaints)).lines["PAINT-1"].efficiency.mean
    assert e1 > e0


def test_defect_share_beta_posterior_and_prior(cfg: TwinConfig) -> None:
    shifts = [_shift("PAINT-1", i, apt_min=480, exits=100, defects=5) for i in range(6)]
    shifts += [_shift("QC-1", i, apt_min=480, exits=10, defects=1) for i in range(3)]
    p = _calibrate(cfg, _inputs(shifts=shifts))
    paint = p.areas["PAINT"]
    assert (paint.rate.source, paint.pq, paint.defects) == ("data", 600, 30)
    assert (paint.rate.alpha, paint.rate.beta) == (31.0, 571.0)
    qc = p.areas["QC"]
    assert qc.rate.source == "prior"
    assert qc.pq == 30
    k = cfg.simulation.forecast.priors.defect_concentration
    base = cfg.simulation.defects.per_area["QC"].base
    assert qc.rate.alpha == pytest.approx(base * k + 3)


def test_rework_minutes_weighted_by_codes(cfg: TwinConfig) -> None:
    p = _calibrate(cfg, _inputs(codes={"WELD-1": {"W-SPOT": 3, "W-GEOM": 1}}))
    expected = (3 * cfg.defects["W-SPOT"].rework_min + cfg.defects["W-GEOM"].rework_min) / 4
    assert p.lines["WELD-1"].rework_mean_min == pytest.approx(expected)


def test_filter_rate_from_consecutive_swaps(cfg: TwinConfig) -> None:
    pf = cfg.simulation.paint_filters
    assert pf is not None
    reason = pf.replacement.reason
    # swaps on consecutive working days (Mon..Fri) = 16 working hours per life
    tz = cfg.timezone
    starts = [datetime(2026, 9, 21 + i, 9, 0, tzinfo=tz) for i in range(5)]
    stops = [Stop("BOOTH-01", s, s + timedelta(minutes=40), False, reason) for s in starts]
    flt = _calibrate(cfg, _inputs(stops=stops)).filters
    assert flt is not None
    assert flt.n_lives == 4
    assert flt.rate_source == "data"
    life_h = 16 - 40 / 60
    assert flt.rate_mean_pa_h == pytest.approx((pf.dp_limit_pa - pf.dp_start_pa) / life_h)
    assert flt.replacement.source == "data"
    few = _calibrate(cfg, _inputs(stops=stops[:2])).filters
    assert few is not None
    assert few.rate_source == "prior"
    assert few.rate_mean_pa_h == pf.dp_rate_pa_per_h.mean


def test_calibration_recovers_the_virtual_plant(cfg: TwinConfig, history: History) -> None:
    p = history.params()
    assert p.working_days == 20
    assert not p.warnings
    targets = cfg.simulation.calibration_targets
    for area, (low, high) in targets.defect_rate.items():
        assert low <= p.areas[area].rate.mean <= high, area
    robot = 1 / cfg.simulation.failures["robot"].mtbf_h
    rates = [p.equipment[c].failures.per_h for c in ("ABB-01", "ABB-02", "ABB-03", "ABB-04")]
    assert 0.6 * robot < sum(rates) / 4 < 2.0 * robot  # wear raises the rate above lambda0
    for line in cfg.flow_lines:
        assert 0.94 < p.lines[line].efficiency.mean < 0.995, line
    flt = p.filters
    assert flt is not None
    assert flt.rate_source == "data"
    assert abs(flt.rate_mean_pa_h - cfg.simulation.paint_filters.dp_rate_pa_per_h.mean) < 1.0  # type: ignore[union-attr]
    # JSON round trip (calibration_snapshot.params)
    assert CalibrationParams.model_validate(p.model_dump(mode="json")) == p
