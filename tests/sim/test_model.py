"""Invariants of the virtual plant model (SPEC §5.3, §5.5, §6.2–6.6) on the calibration run."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from itertools import pairwise

import pytest

from qost_sim.model import PlantModel, Rec
from qost_sim.model.equipment import Down, Unit
from sim_support import CalibrationRun, of
from twin_core.config import TwinConfig
from twin_core.config.simulation import FailureInject, SetStateInject
from twin_core.domain import EquipmentState
from twin_core.events import FIRST_EXIT_RESULTS

BODY_ID = re.compile(r"^B\d{6}\d{4}$")


def intervals(records: list[Rec], entity: str) -> list[tuple[float, float, str, str | None]]:
    states = [r for r in records if r.kind == "state" and r.entity == entity]
    out = []
    for cur, nxt in pairwise(states):
        out.append((cur.t, nxt.t, cur.data["state"], cur.data.get("reason_code")))
    return out


def test_records_are_time_ordered_and_states_change(calibration_run: CalibrationRun) -> None:
    _report, _model, records = calibration_run
    times = [r.t for r in records]
    assert times == sorted(times)
    last: dict[str, dict[str, object]] = {}
    for rec in records:
        if rec.kind != "state":
            continue
        assert rec.data != last.get(rec.entity), f"repeated state {rec.entity} at {rec.t}"
        last[rec.entity] = rec.data
    assert not [r for r in records if r.kind == "void"]


def test_counters_match_unit_events(calibration_run: CalibrationRun) -> None:
    _report, model, records = calibration_run
    units = of(records, "unit")
    for code, line in model.lines.items():
        mine = [u for u in units if u.entity == code]
        first = [u for u in mine if u.data["result"] in FIRST_EXIT_RESULTS]
        assert line.produced == len(first)
        assert line.good == sum(1 for u in first if u.data["result"] == "pass")
        assert line.reject == sum(1 for u in first if u.data["result"] != "pass")
        assert mine[-1].extra is not None
        assert mine[-1].extra["produced"] == line.produced


def test_bodies_flow_in_order_and_are_conserved(
    cfg: TwinConfig, calibration_run: CalibrationRun
) -> None:
    _report, model, records = calibration_run
    order = {line: i for i, line in enumerate(cfg.flow_lines)}
    seen: dict[str, list[int]] = defaultdict(list)
    ids: Counter[tuple[str, str]] = Counter()
    for rec in of(records, "unit"):
        body = rec.data["body_id"]
        assert BODY_ID.match(body), body
        if rec.data["result"] in FIRST_EXIT_RESULTS:
            seen[body].append(order[rec.data["line"]])
            ids[(body, rec.data["line"])] += 1
    assert max(ids.values()) == 1  # one first exit per body and line
    assert all(path == sorted(path) for path in seen.values())
    c = model.counters
    assert model.ckd.consumed == c.bodies - c.wip_initial
    assert model.wip() == c.bodies - c.fg - c.scrapped >= 0
    last = cfg.flow_lines[-1]
    fg = sum(1 for r in of(records, "unit", last) if r.data["result"] in ("pass", "rework_pass"))
    assert fg == c.fg
    consumed = [r for r in of(records, "ckd") if r.data["event"] == "consume"]
    assert len(consumed) == model.ckd.consumed


def test_buffers_stay_within_capacity(calibration_run: CalibrationRun) -> None:
    _report, model, records = calibration_run
    for rec in of(records, "buffer_level"):
        assert 0 <= rec.data["level"] <= rec.data["capacity"]
    assert {b.level for b in model.buffers.values()} <= set(range(31))


def test_no_production_outside_shifts(cfg: TwinConfig, calibration_run: CalibrationRun) -> None:
    _report, model, records = calibration_run
    end = model.now
    shifts = cfg.calendar.shifts_between(model.t0, end, working_only=True)
    spans = [(model.sec(s.start), model.sec(s.end)) for s in shifts]
    for rec in of(records, "unit"):
        assert any(s0 <= rec.t <= s1 for s0, s1 in spans), model.at(rec.t)


def test_microstops_are_short_and_failures_use_configured_reasons(
    cfg: TwinConfig, calibration_run: CalibrationRun
) -> None:
    _report, model, records = calibration_run
    threshold = cfg.rules.thresholds.microstop_threshold_s
    sim = cfg.simulation
    durations: dict[str, list[float]] = defaultdict(list)
    for code, unit in model.units.items():
        allowed = set(sim.failures[unit.type].reasons) if unit.type in sim.failures else set()
        fm = sim.failures.get(unit.type)
        if fm is not None and fm.chain_break is not None:
            allowed.add(fm.chain_break.reason)
        if unit.type in sim.microstops:
            allowed.add(sim.microstops[unit.type].reason)
        if unit.filters is not None:
            allowed.add(unit.filters.replacement.reason)
        for start, end, state, reason in intervals(records, code):
            if state == EquipmentState.DOWN_UNPLANNED.value:
                assert reason in allowed, (code, reason)
                durations[f"{unit.type}:{reason}"].append(end - start)
    for type_code, micro in sim.microstops.items():
        values = durations[f"{type_code}:{micro.reason}"]
        if type_code == "conveyor":
            values = [v for v in values if v < threshold] or values  # ME-JAM is micro only here
        assert values, type_code
        assert max(values) < threshold
    assert durations["booth:MT-FILTER"], "filters were never replaced"


def test_planned_maintenance_schedule(cfg: TwinConfig, calibration_run: CalibrationRun) -> None:
    _report, model, records = calibration_run
    tz = model.tz
    for pm in cfg.simulation.planned_maintenance:
        shift = next(s for s in cfg.plant.calendar.shifts if s.code == pm.shift)
        for unit in model.units_by_type[pm.equipment_type]:
            planned = [
                (s, e)
                for s, e, state, reason in intervals(records, unit.code)
                if state == EquipmentState.DOWN_PLANNED.value and reason == pm.reason
            ]
            assert planned, unit.code
            days = []
            for start, end in planned:
                local = model.at(start).astimezone(tz)
                assert local.time() == shift.start
                assert end - start == pytest.approx(pm.duration_min * 60)
                days.append(model.working_day_index(local.date()))
            assert all((d + unit.index) % pm.every_working_days == 0 for d in days)
            assert all(b - a == pm.every_working_days for a, b in pairwise(days))


def test_no_planned_maintenance_on_demo_morning_for_scenario_units(cfg: TwinConfig) -> None:
    """S1/S2/S3 must not collide with planned maintenance at 07:00 on Demo Day."""
    model = PlantModel(cfg, start=cfg.simulation.clock.backfill_from)
    demo_day = cfg.simulation.clock.demo_start.astimezone(model.tz).date()
    wd = model.working_day_index(demo_day)
    targets = {"CONV-03", "BOOTH-02", "ABB-04"}
    for pm in cfg.simulation.planned_maintenance:
        for unit in model.units_by_type[pm.equipment_type]:
            if unit.code in targets:
                assert (wd + unit.index) % pm.every_working_days != 0, unit.code


def test_wear_resets_after_wear_repair_and_pm(cfg: TwinConfig) -> None:
    start = datetime.combine(
        cfg.simulation.clock.demo_start.date(),
        time(9),
        tzinfo=cfg.simulation.clock.demo_start.tzinfo,
    )
    model = PlantModel(cfg, start=start)
    robot = model.units["ABB-01"]
    params = robot.wear_params
    assert params is not None
    model.apply(
        SetStateInject.model_validate(
            {"type": "set_state", "equipment": "ABB-01", "degradation": 0.9}
        )
    )
    model.run_until(1)
    assert robot.d == pytest.approx(0.9)
    model.apply(
        FailureInject(type="failure", equipment="ABB-01", reason="RB-COLLISION", duration_min=5)
    )
    model.run_until(6 * 60 + 10)
    assert robot.d >= 0.9  # random reason: no reset
    model.apply(FailureInject(type="failure", equipment="ABB-01", reason="RB-TOOL", duration_min=5))
    model.run_until(12 * 60 + 20)
    assert robot.state is EquipmentState.RUNNING
    assert robot.d == pytest.approx(params.reset_after_repair)
    robot.d = 0.5
    robot.command("pm", 60.0, "PM-SCHEDULED")
    model.run_until(model.env.now + 61)
    assert robot.d == pytest.approx(max(params.pm_floor, 0.5 - params.pm_reduction))
    robot.d = 0.06
    robot.command("pm", 60.0, "PM-SCHEDULED")
    model.run_until(model.env.now + 61)
    assert robot.d == pytest.approx(params.pm_floor)


def test_class_b_stop_degrades_the_line_class_c_does_not(cfg: TwinConfig) -> None:
    demo = cfg.simulation.clock.demo_start
    t = demo + timedelta(hours=2)
    model = PlantModel(cfg, start=demo - timedelta(days=1))
    model.schedule(
        model.sec(t),
        FailureInject(type="failure", equipment="ABB-02", reason="RB-COLLISION", duration_min=20),
    )
    model.schedule(
        model.sec(t),
        FailureInject(type="failure", equipment="WATER-01", reason="EL-SENSOR", duration_min=20),
    )
    model.run_until_time(t + timedelta(minutes=10))
    weld, qc = model.lines["WELD-1"], model.lines["QC-1"]
    assert weld.rate == pytest.approx(cfg.equipment["ABB-02"].degraded_capacity)
    assert weld.state in (EquipmentState.DEGRADED, EquipmentState.BLOCKED, EquipmentState.STARVED)
    assert qc.rate == 1.0
    assert qc.state is not EquipmentState.DOWN_UNPLANNED
    records = model.drain()
    assert {r.data["state"] for r in of(records, "state", "WELD-1") if r.t >= model.sec(t)} <= {
        "DEGRADED",
        "BLOCKED",
        "STARVED",
    }
    model.run_until_time(t + timedelta(minutes=30))
    assert weld.rate == 1.0


def _down(unit: Unit) -> Down | None:
    return unit.down


def _arm(model: PlantModel, code: str, *, wear_h: float, random_h: float) -> None:
    """Set the next wear / random failure of a unit to happen after the given operating hours."""
    unit = model.units[code]
    unit.command("set", {})
    model.run_until(model.env.now + 1e-3)  # the unit restarts its wait now
    unit.e_wear = unit.lam_w() * wear_h * 3600
    unit.e_rand = unit.lam_r * random_h * 3600
    unit.command("set", {})
    model.run_until(model.env.now + 1e-3)


def test_precursor_only_before_wear_failures(cfg: TwinConfig) -> None:
    """The oven drift (precursor) precedes wear-reason failures only (SPEC §11.1)."""
    demo = cfg.simulation.clock.demo_start
    model = PlantModel(cfg, start=demo + timedelta(hours=1), telemetry_period_s=300)
    oven = model.units["OVEN-01"]
    assert oven.wear_reasons == {"EL-DRIVE"}
    model.run_until(1)
    _arm(model, "OVEN-01", wear_h=100, random_h=0.5)  # random failure in 30 operating minutes
    model.run_until(model.env.now + 1800 + 120)
    recs = model.drain()
    stops = [r for r in of(recs, "state", "OVEN-01") if r.data["state"] == "DOWN_UNPLANNED"]
    assert stops[0].data["reason_code"] == "EL-SENSOR"
    temps = [r.data["value"] for r in of(recs, "telemetry", "OVEN-01")]
    assert max(temps) < 140 + 5 * 1.2  # no drift before a random failure
    model.run_until(model.env.now + 3600)
    model.drain()
    assert oven.down is None
    _arm(model, "OVEN-01", wear_h=1.5, random_h=100)  # wear failure in 1.5 operating hours
    assert oven.precursor(2) == pytest.approx(0.25, abs=0.01)
    model.run_until(model.env.now + 1.5 * 3600 - 60)
    assert oven.precursor(2) > 0.95
    recs = model.drain()
    late = [r.data["value"] for r in of(recs, "telemetry", "OVEN-01")][-3:]
    assert min(late) > 145
    model.run_until(model.env.now + 120)
    stop = _down(oven)
    assert stop is not None
    assert stop.reason == "EL-DRIVE"
    assert oven.precursor(2) == 0.0
