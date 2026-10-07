"""FR-SIM-03: the virtual plant reproduces the case data (simulation.yaml calibration_targets)."""

from __future__ import annotations

from sim_support import CalibrationRun
from twin_core.config import TwinConfig


def test_calibration_targets(cfg: TwinConfig, calibration_run: CalibrationRun) -> None:
    report, _model, _records = calibration_run
    targets = cfg.simulation.calibration_targets
    print("\n" + report.render())
    assert report.working_days == 20
    assert report.working_shifts == 40
    assert targets.throughput_per_shift.min <= report.throughput_per_shift
    assert report.throughput_per_shift <= targets.throughput_per_shift.max
    for area, (low, high) in targets.defect_rate.items():
        assert low <= report.defect_rate[area] <= high, area
    band = targets.unplanned_downtime_min_per_area_day
    for area in targets.defect_rate:
        assert band.min <= report.unplanned_min_per_day[area] <= band.max, area
    assert all(m <= band.max for m in report.unplanned_min_per_day.values())
    assert report.bottleneck == targets.expected_bottleneck
    assert report.problems == []


def test_bottleneck_is_paint_with_weld_close(calibration_run: CalibrationRun) -> None:
    """Expected bottleneck PAINT-1 (filters + repaint); WELD-1 the runner-up (case data)."""
    report, _model, _records = calibration_run
    share = report.flow_loss_share
    assert share["PAINT-1"] == min(share.values())
    assert share["QC-1"] == max(share.values())  # the fastest line starves most
