"""Shared plant schedules (twin_core.schedule): working-day index, PM, CKD lots."""

from __future__ import annotations

from datetime import date

from twin_core.config import TwinConfig
from twin_core.schedule import (
    ckd_daily_plan,
    ckd_lot_sizes,
    equipment_positions,
    is_delivery_day,
    pm_due,
    schedule_anchor,
    working_day_index,
)


def test_working_day_index(cfg: TwinConfig) -> None:
    anchor = schedule_anchor(cfg)
    assert anchor == date(2026, 9, 1)
    cal = cfg.calendar
    assert working_day_index(cal, anchor, anchor) == 0
    assert working_day_index(cal, anchor, date(2026, 9, 8)) == 5  # Tue..Mon, weekend skipped
    assert working_day_index(cal, anchor, date(2026, 8, 31)) == -1
    assert working_day_index(cal, anchor, date(2026, 8, 29)) == -1  # Saturday -> Monday


def test_pm_due_matches_the_demo_morning(cfg: TwinConfig) -> None:
    wd = working_day_index(cfg.calendar, schedule_anchor(cfg), date(2026, 10, 16))
    due = {t.equipment for t in pm_due(cfg, shift_code="A", working_day=wd)}
    # S1-S3 of the demo need these units free at 16.10 07:00 (see the M2 decision)
    assert not due & {"CONV-03", "BOOTH-02", "ABB-04"}
    assert pm_due(cfg, shift_code="B", working_day=wd) == []
    positions = equipment_positions(cfg)
    for task in pm_due(cfg, shift_code="A", working_day=wd + 1):
        pm = next(p for p in cfg.simulation.planned_maintenance if p.reason == task.reason)
        assert (wd + 1 + positions[task.equipment]) % pm.every_working_days == 0


def test_ckd_lots(cfg: TwinConfig) -> None:
    assert ckd_lot_sizes(cfg, date(2026, 10, 5)) == {"ONIX": 357, "COBALT": 257, "J7": 71}
    sept = ckd_daily_plan(cfg, date(2026, 9, 10))  # no plan for September: line rate x mix
    assert sept["ONIX"] == 120 * 2 * cfg.simulation.process.product_mix["ONIX"]
    every = cfg.simulation.ckd_supply.delivery_every_working_days
    assert is_delivery_day(cfg, 0)
    assert is_delivery_day(cfg, every)
    assert not is_delivery_day(cfg, 1)
