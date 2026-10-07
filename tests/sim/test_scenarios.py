"""Effects of the demo scenarios S1–S5 (simulation.yaml ``scenarios``, SPEC §6.9, §17).

Every scenario run is compared with a baseline run of the same seed: the model uses named random
streams per entity, so both runs share their random numbers (common random numbers).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from qost_sim.live import warm_model
from qost_sim.model import PlantModel, Rec
from qost_sim.model.scenarios import InjectError, check_configured_scenarios, parse_inject
from sim_support import of, run
from support import mutate
from twin_core.config import TwinConfig, load_config
from twin_core.config.simulation import DefectMultiplierInject, SetStateInject
from twin_core.events import FIRST_EXIT_RESULTS

HOUR = timedelta(hours=1)


def demo(cfg: TwinConfig) -> datetime:
    return cfg.simulation.clock.demo_start


def level_at(records: list[Rec], buffer: str, t: float) -> int:
    levels = [r for r in of(records, "buffer_level", buffer) if r.t <= t]
    return int(levels[-1].data["level"])


def first_exits(records: list[Rec], line: str, t0: float, t1: float) -> list[Rec]:
    return [
        r
        for r in of(records, "unit", line)
        if t0 < r.t < t1 and r.data["result"] in FIRST_EXIT_RESULTS
    ]


# --------------------------------------------------------------------------- S1


def test_s1_chain_break_stops_assembly_for_55_minutes(cfg: TwinConfig) -> None:
    scenario = cfg.scenarios["S1-CHAIN-BREAK"]
    inject_at = demo(cfg) + 2 * HOUR
    window = {"start": demo(cfg) - timedelta(days=3), "until": demo(cfg) + 8 * HOUR}
    base_model, base = run(cfg, **window)  # type: ignore[arg-type]
    model, recs = run(cfg, injects=[(inject_at, scenario.inject)], **window)  # type: ignore[arg-type]
    t = model.sec(inject_at)
    end = t + 55 * 60

    conv = [r for r in of(recs, "state", "CONV-03") if r.t >= t - 1e-6]
    assert conv[0].t == pytest.approx(t)
    assert conv[0].data == {
        "state": "DOWN_UNPLANNED",
        "reason_code": "ME-CHAIN",
        "alarm_code": "ME-CHAIN",
    }
    assert conv[1].t == pytest.approx(end)
    assert conv[1].data == {"state": "RUNNING"}
    alarms = [r for r in of(recs, "alarm", "CONV-03") if r.t >= t - 1e-6]
    assert alarms[0].data["code"] == "ME-CHAIN"
    assert alarms[0].data["active"] is True
    assert alarms[1].t == pytest.approx(end)
    assert alarms[1].data["active"] is False
    assy = [r for r in of(recs, "state", "ASSY-1") if r.t == pytest.approx(t)]
    assert assy[-1].data == {"state": "DOWN_UNPLANNED", "reason_code": "ME-CHAIN"}

    assert first_exits(recs, "ASSY-1", t + 1, end) == []
    assert level_at(recs, "PBS", end) > level_at(base, "PBS", end)
    shift_end = model.sec(demo(cfg) + 8 * HOUR)
    lost = len(first_exits(base, "ASSY-1", t, shift_end)) - len(
        first_exits(recs, "ASSY-1", t, shift_end)
    )
    ideal = 55 * 60 / cfg.lines["ASSY-1"].ict_seconds  # 14.16 cars (SPEC §5.7)
    assert ideal - 6 <= lost <= ideal + 2
    assert base_model.sec(inject_at) == t


# --------------------------------------------------------------------------- S2


def test_s2_filter_reaches_its_limit_at_the_predicted_operating_hour(cfg: TwinConfig) -> None:
    scenario = cfg.scenarios["S2-FILTER-TREND"]
    pf = cfg.simulation.paint_filters
    assert pf is not None
    inject_at = demo(cfg) + 2 * HOUR
    model = PlantModel(cfg, start=demo(cfg) - timedelta(days=2), telemetry_period_s=300)
    model.schedule(model.sec(inject_at), scenario.inject)
    model.run_until(model.sec(inject_at) + 1)
    booth = model.units["BOOTH-02"]
    assert booth.filter_dp() == pytest.approx(370, abs=0.1)
    rate_h = booth.filter_rate_s * 3600
    predicted_h = (pf.dp_limit_pa - 370) / rate_h
    model.run_until_time(inject_at + timedelta(days=3))
    recs = model.drain()
    t = model.sec(inject_at)

    stops = [
        r
        for r in of(recs, "state", "BOOTH-02")
        if r.t > t and r.data.get("reason_code") == pf.replacement.reason
    ]
    assert stops, "the filter was not replaced"
    t_rep = stops[0].t
    states = of(recs, "state", "BOOTH-02")
    operating = 0.0
    for cur, nxt in pairwise(states):
        lo, hi = max(cur.t, t), min(nxt.t, t_rep)
        if hi > lo and cur.data["state"] == "RUNNING":
            operating += hi - lo
    assert operating / 3600 == pytest.approx(predicted_h, abs=0.01)
    samples = [r for r in of(recs, "telemetry", "BOOTH-02") if r.data["signal"] == pf.signal]
    after = [r.data["value"] for r in samples if r.t > t]
    assert after[0] == pytest.approx(370, abs=20)
    before_rep = [r.data["value"] for r in samples if t < r.t < t_rep]
    assert max(before_rep) > pf.dp_limit_pa - 25


# --------------------------------------------------------------------------- S3


def test_s3_wear_shows_in_signals_and_brings_failures_forward(cfg: TwinConfig) -> None:
    scenario = cfg.scenarios["S3-ABB04-WEAR"]
    inject_at = demo(cfg) + 2 * HOUR
    window = {"start": demo(cfg) - timedelta(days=1), "until": inject_at + 2 * HOUR}
    _, base = run(cfg, telemetry_period_s=300, **window)  # type: ignore[arg-type]
    model, recs = run(
        cfg,
        injects=[(inject_at, scenario.inject)],
        telemetry_period_s=300,
        **window,  # type: ignore[arg-type]
    )
    t = model.sec(inject_at)
    oracle = [r for r in of(recs, "oracle", "ABB-04") if r.t == pytest.approx(t)]
    assert oracle[0].data["degradation"] == pytest.approx(0.75)

    def current(records: list[Rec]) -> float:
        """Mean motor current after the inject and before ABB-04's next stop."""
        stops = [r.t for r in of(records, "state", "ABB-04") if r.t > t]
        until = stops[0] if stops else float("inf")
        return statistics.fmean(
            r.data["value"]
            for r in of(records, "telemetry", "ABB-04")
            if t <= r.t < until and r.data["signal"] == "motor_current_a"
        )

    assert current(recs) == pytest.approx(12 + 7 * 0.75, abs=0.4)
    assert current(recs) - current(base) > 2.5

    wear = set(cfg.simulation.failures["robot"].wear_reasons)

    def wear_failure_within_8h(injected: bool, seed: int) -> bool:
        injects = [(inject_at, scenario.inject)] if injected else []
        m, r = run(
            cfg,
            start=demo(cfg) - timedelta(days=1),
            until=inject_at + 8 * HOUR,
            seed=seed,
            injects=injects,
        )
        return any(
            x.t > m.sec(inject_at) and x.data.get("reason_code") in wear
            for x in of(r, "state", "ABB-04")
        )

    seeds = range(12)
    with_wear = sum(wear_failure_within_8h(True, s) for s in seeds)
    without = sum(wear_failure_within_8h(False, s) for s in seeds)
    assert with_wear >= len(seeds) / 3
    assert with_wear > without


# --------------------------------------------------------------------------- S4


def test_s4_bad_lot_multiplies_defects_for_its_duration(cfg: TwinConfig) -> None:
    scenario = cfg.scenarios["S4-BAD-CKD-LOT"]
    inject = scenario.inject
    assert isinstance(inject, DefectMultiplierInject)
    inject_at = demo(cfg) + HOUR
    lines = [ln for ln in cfg.flow_lines if cfg.area_of_line(ln).code in inject.areas]
    totals = {"base": 0, "scenario": 0, "after": 0, "after_base": 0}
    for seed in range(5):
        window = {
            "start": demo(cfg) - timedelta(days=1),
            "until": inject_at + timedelta(minutes=inject.duration_min) + 2 * HOUR,
            "seed": seed,
        }
        model, base = run(cfg, **window)  # type: ignore[arg-type]
        _, recs = run(cfg, injects=[(inject_at, inject)], **window)  # type: ignore[arg-type]
        t0 = model.sec(inject_at)
        t1 = t0 + inject.duration_min * 60
        for line in lines:
            for name, records, lo, hi in (
                ("base", base, t0, t1),
                ("scenario", recs, t0, t1),
                ("after_base", base, t1 + 1800, t1 + 7200),
                ("after", recs, t1 + 1800, t1 + 7200),
            ):
                totals[name] += sum(
                    1 for r in first_exits(records, line, lo, hi) if r.data["result"] != "pass"
                )
    assert totals["base"] > 10
    assert 1.3 <= totals["scenario"] / totals["base"] <= 2.0
    assert totals["after"] <= totals["after_base"] * 1.25 + 2  # the multiplier expired


# --------------------------------------------------------------------------- S5


def _kit_runs(cfg: TwinConfig) -> tuple[PlantModel, list[Rec], list[Rec], float]:
    scenario = cfg.scenarios["S5-KIT-SHORTAGE"]
    inject_at = demo(cfg) + HOUR
    window = {"start": demo(cfg) - timedelta(days=1), "until": inject_at + timedelta(days=8)}
    _, base = run(cfg, **window)  # type: ignore[arg-type]
    model, recs = run(cfg, injects=[(inject_at, scenario.inject)], **window)  # type: ignore[arg-type]
    return model, base, recs, model.sec(inject_at)


def _deliveries(records: list[Rec], product: str, after: float) -> list[float]:
    return [
        r.t for r in of(records, "ckd", product) if r.data["event"] == "delivery" and r.t > after
    ]


def test_s5_kit_shortage_resequences_and_delays_the_next_lot(cfg: TwinConfig) -> None:
    assert cfg.simulation.process.ckd_shortage_policy == "resequence"
    model, base, recs, t = _kit_runs(cfg)
    sets = [r for r in of(recs, "ckd", "J7") if r.data["event"] == "set" and r.t >= t]
    assert sets[0].data["kits"] == 40
    empty = [
        r.t for r in of(recs, "ckd", "J7") if r.data["event"] == "consume" and r.data["kits"] == 0
    ]
    assert empty, "J7 kits never ran out"
    t0 = empty[0]
    base_lots = _deliveries(base, "J7", t)
    scen_lots = _deliveries(recs, "J7", t)
    delayed = base_lots[0] + 4 * 86_400
    assert base_lots[0] not in scen_lots
    assert any(abs(x - delayed) < 1e-3 for x in scen_lots)
    restock = min(x for x in scen_lots if x > t0)
    window = first_exits(recs, "WELD-1", t0 + 900, restock)
    assert window, "WELD-1 must keep building other models"
    assert {r.data["product"] for r in window} == {"ONIX", "COBALT"}
    starved = [
        r
        for r in of(recs, "state", "WELD-1")
        if t0 < r.t < restock and r.data.get("reason_code") == "MAT-SHORTAGE"
    ]
    assert starved == []
    resumed = [
        r for r in first_exits(recs, "WELD-1", restock, model.env.now) if r.data["product"] == "J7"
    ]
    assert resumed, "J7 production resumes after the delayed lot"


def test_s5_with_wait_policy_starves_the_first_line(config_copy: Path) -> None:
    mutate(
        config_copy / "simulation.yaml",
        "ckd_shortage_policy: resequence",
        "ckd_shortage_policy: wait",
    )
    cfg = load_config(config_copy, tag_map=False)
    _model, _base, recs, t = _kit_runs(cfg)
    empty = [
        r.t for r in of(recs, "ckd", "J7") if r.data["event"] == "consume" and r.data["kits"] == 0
    ]
    starved = [
        r
        for r in of(recs, "state", "WELD-1")
        if r.t > empty[0] and r.data == {"state": "STARVED", "reason_code": "MAT-SHORTAGE"}
    ]
    assert starved
    restock = min(x for x in _deliveries(recs, "J7", t) if x > empty[0])
    assert first_exits(recs, "WELD-1", starved[0].t + 900, restock) == []


# --------------------------------------------------------------------------- schedule / inject


def test_at_min_scenarios_are_applied_at_demo_start(cfg: TwinConfig) -> None:
    model = warm_model(cfg)
    scheduled = {i.scenario_id: i.t for i in model.interventions}
    demo_s = model.sec(demo(cfg))
    expected = {s.id for s in cfg.simulation.scenarios if s.at_min is not None}
    assert set(scheduled) == expected == {"S2-FILTER-TREND", "S3-ABB04-WEAR"}
    assert all(t == pytest.approx(demo_s) for t in scheduled.values())
    model.run_until(model.env.now + 1)
    assert model.units["BOOTH-02"].filter_dp() == pytest.approx(370, abs=0.5)
    assert model.units["ABB-04"].d == pytest.approx(0.75)


def test_configured_scenarios_are_supported(cfg: TwinConfig) -> None:
    assert check_configured_scenarios(cfg) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            {"type": "failure", "equipment": "CONV-3", "reason": "ME-CHAIN", "duration_min": 5},
            "unknown equipment",
        ),
        (
            {"type": "failure", "equipment": "CONV-03", "reason": "PM-CLEANING", "duration_min": 5},
            "planned",
        ),
        (
            {"type": "failure", "equipment": "CONV-03", "reason": "NOPE", "duration_min": 5},
            "unknown reason",
        ),
        ({"type": "set_state", "equipment": "BOOTH-02", "humidity_pct": 80}, "cannot be set"),
        ({"type": "set_state", "equipment": "BOOTH-02", "degradation": 0.5}, "cannot be set"),
        (
            {"type": "defect_multiplier", "areas": ["FG"], "factor": 2, "duration_min": 5},
            "production area",
        ),
        ({"type": "ckd", "product": "LADA"}, "unknown product"),
        ({"type": "ckd", "product": "J7"}, "set_kits and/or"),
        ({"type": "explode"}, "type"),
        ({"type": "failure", "equipment": "CONV-03"}, "reason"),
    ],
)
def test_ad_hoc_inject_validation(cfg: TwinConfig, raw: dict[str, object], message: str) -> None:
    with pytest.raises(InjectError) as caught:
        parse_inject(cfg, raw)
    assert any(message in p for p in caught.value.problems), caught.value.problems


def test_valid_ad_hoc_injects(cfg: TwinConfig) -> None:
    inject = parse_inject(cfg, {"type": "set_state", "equipment": "CONV-02", "degradation": 0.9})
    assert isinstance(inject, SetStateInject)
    assert inject.values == {"degradation": 0.9}
    filt = parse_inject(cfg, {"type": "set_state", "equipment": "BOOTH-01", "filter_dp_pa": 420})
    assert isinstance(filt, SetStateInject)
