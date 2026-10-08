"""What-if overrides FR-FC-02: schema bounds, codes with suggestions, extra-shift rules."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from twin_core.config import TwinConfig
from twin_core.forecast.calibration import month_bounds
from twin_core.forecast.overrides import ExtraShift, Overrides, check_overrides


def _issues(cfg: TwinConfig, ov: Overrides, as_of: datetime | None = None) -> list[str]:
    m0, m1 = month_bounds(cfg, "2026-10")
    found = check_overrides(
        cfg, ov, month_start=m0, month_end=m1, as_of=as_of or cfg.simulation.clock.demo_start
    )
    return [f"{'.'.join(map(str, i.loc))}: {i.msg}" for i in found]


def test_empty_and_valid_overrides(cfg: TwinConfig) -> None:
    assert Overrides().is_empty()
    ov = Overrides(
        defect_rate={"PAINT": 0.03},
        mtbf_multiplier={"A": 1.2, "CONV-03": 2.0},
        mttr_multiplier={"B": 0.8},
        ict_seconds={"PAINT-1": 225},
        buffer_capacity={"PBS": 40},
        extra_shifts=[ExtraShift(date=date(2026, 10, 17)), ExtraShift(date=date(2026, 10, 26))],
        filter_policy="predictive_shift_change",
        ckd_delay_days={"J7": 4},
    )
    assert _issues(cfg, ov) == []
    assert not ov.is_empty()
    assert Overrides.model_validate(ov.normalized()) == ov


@pytest.mark.parametrize(
    "payload",
    [
        {"defect_rate": {"PAINT": 0.7}},
        {"defect_rate": {"PAINT": -0.1}},
        {"mtbf_multiplier": {"A": 0}},
        {"mttr_multiplier": {"A": 20}},
        {"ict_seconds": {"PAINT-1": 5}},
        {"buffer_capacity": {"PBS": -1}},
        {"ckd_delay_days": {"J7": 40}},
        {"filter_policy": "sometimes"},
        {"unknown_field": 1},
    ],
)
def test_schema_bounds(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Overrides.model_validate(payload)


def test_unknown_codes_get_a_suggestion(cfg: TwinConfig) -> None:
    issues = _issues(
        cfg,
        Overrides(
            defect_rate={"PAINTT": 0.03},
            mtbf_multiplier={"CONV-3": 2.0},
            ict_seconds={"WELD-2": 240},
            buffer_capacity={"PBZ": 40},
            ckd_delay_days={"ONYX": 2},
        ),
    )
    assert "defect_rate.PAINTT: unknown area 'PAINTT'; did you mean 'PAINT'?" in issues
    assert (
        "mtbf_multiplier.CONV-3: unknown equipment or class 'CONV-3'; did you mean 'CONV-03'?"
        in issues
    )
    assert any(i.startswith("ict_seconds.WELD-2: unknown line 'WELD-2'") for i in issues)
    assert any(i.startswith("buffer_capacity.PBZ: unknown buffer") for i in issues)
    assert any(i.startswith("ckd_delay_days.ONYX: unknown product 'ONYX'") for i in issues)
    assert len(issues) == 5


def test_extra_shift_rules(cfg: TwinConfig) -> None:
    def one(item: ExtraShift, as_of: datetime | None = None) -> list[str]:
        return _issues(cfg, Overrides(extra_shifts=[item]), as_of)

    assert one(ExtraShift(date=date(2026, 11, 7))) == [
        "extra_shifts.0.date: 2026-11-07 is outside the forecast month"
    ]
    worked = one(ExtraShift(date=date(2026, 10, 19), shifts=["A"]))
    assert worked
    assert "already worked" in worked[0]
    assert one(ExtraShift(date=date(2026, 10, 17), shifts=["C"]))[0].startswith(
        "extra_shifts.0.shifts.0: unknown shift 'C'"
    )
    past = one(ExtraShift(date=date(2026, 10, 10), shifts=["A"]))
    assert past
    assert "starts before the forecast" in past[0]
    twice = _issues(
        cfg,
        Overrides(
            extra_shifts=[ExtraShift(date=date(2026, 10, 17)), ExtraShift(date=date(2026, 10, 17))]
        ),
    )
    assert twice == ["extra_shifts.1.date: 2026-10-17 is listed twice"]


def test_merged_overrides() -> None:
    a = Overrides(defect_rate={"PAINT": 0.03}, mtbf_multiplier={"A": 1.2})
    b = Overrides(mtbf_multiplier={"B": 1.5}, filter_policy="predictive_shift_change")
    m = a.merged(b)
    assert m.mtbf_multiplier == {"A": 1.2, "B": 1.5}
    assert m.defect_rate == {"PAINT": 0.03}
    assert m.filter_policy == "predictive_shift_change"
