"""Engine core (SPEC §9): states, downtime, KPI close and versions, rules, escalation, snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pytest
from engine_support import (
    LINES,
    SHIFT_A,
    SHIFT_B,
    at,
    buffer,
    ev,
    of_type,
    run,
    start_plant,
    state,
    unit,
)

from qost_engine.core import (
    AlertEscalate,
    AlertUpsert,
    AuditRow,
    BottleneckRow,
    DowntimeRow,
    DqUpsert,
    Effect,
    EngineCore,
    KpiShiftRow,
    StateInterval,
)
from qost_engine.writer import coalesce
from twin_core.config import TwinConfig


def latest_alerts(effects: Sequence[object]) -> dict[str, AlertUpsert]:
    out: dict[str, AlertUpsert] = {}
    for e in effects:
        if isinstance(e, AlertUpsert):
            out[e.dedup_key] = e
    return out


def test_state_intervals_and_idempotence(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="replay")
    effects = run(core, start_plant(cfg))
    effects += run(core, [state("CONV-03", "RUNNING", at(5))])  # re-statement: no-op
    effects += run(core, [state("CONV-03", "DOWN_UNPLANNED", at(10), alarm_code="ME-CHAIN")])
    effects += run(core, [state("CONV-03", "RUNNING", at(70))])
    conv = [e for e in of_type(coalesce(effects), StateInterval) if e.entity == "CONV-03"]
    assert [(e.state, e.start, e.end) for e in sorted(conv, key=lambda e: e.start)] == [
        ("RUNNING", SHIFT_A, at(10)),
        ("DOWN_UNPLANNED", at(10), at(70)),
        ("RUNNING", at(70), None),
    ]
    assert conv[-2].reason_code == "ME-CHAIN"
    # a late event (older than the last applied one) is ignored
    before = core.stats["late_events"]
    run(core, [state("CONV-03", "DOWN_PLANNED", at(30))])
    assert core.stats["late_events"] == before + 1
    assert core.st.entities["CONV-03"].state == "RUNNING"


def test_downtime_reason_chain_microstops_and_classification_flag(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="replay")
    run(core, start_plant(cfg))
    effects = run(
        core,
        [
            state("CONV-01", "DOWN_UNPLANNED", at(10), reason_code="ME-JAM"),
            state("CONV-01", "RUNNING", at(12)),  # 2 min: microstop
            state("CONV-02", "DOWN_UNPLANNED", at(20), alarm_code="Обрыв цепи"),  # alias
            state("CONV-02", "RUNNING", at(40)),
            state("JIG-01", "DOWN_UNPLANNED", at(50), alarm_code="E-WHAT"),  # unknown code
            state("JIG-01", "RUNNING", at(65)),
            state("ABB-02", "DOWN_PLANNED", at(70), alarm_code="PM-SCHEDULED"),
        ],
    )
    rows = {(d.entity, d.start): d for d in of_type(effects, DowntimeRow)}
    micro = rows[("CONV-01", at(10))]
    assert micro.microstop
    assert micro.duration_s == pytest.approx(120)
    assert rows[("CONV-02", at(20))].reason_code == "ME-CHAIN"
    assert not rows[("CONV-02", at(20))].microstop
    unk = rows[("JIG-01", at(50))]
    assert unk.reason_code == "UNK"
    assert core.st.stops[f"JIG-01|{at(50).timestamp()!r}"].flagged  # >= 10 min without a reason
    assert any(
        d.rule_id == "DQ-07" and d.details["value"] == "E-WHAT" for d in of_type(effects, DqUpsert)
    )
    pm = rows[("ABB-02", at(70))]
    assert pm.planned
    assert pm.reason_code == "PM-SCHEDULED"
    assert pm.shift_code == "A"


def test_line_reason_from_earliest_class_a_unit_even_when_it_arrives_later(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="replay")
    run(core, start_plant(cfg))
    effects = run(
        core,
        [
            state("ASSY-1", "DOWN_UNPLANNED", at(30), line=True),  # line first (OPC UA batch order)
            state("CONV-03", "DOWN_UNPLANNED", at(30), alarm_code="ME-CHAIN"),
        ],
    )
    line_rows = [d for d in of_type(effects, DowntimeRow) if d.entity == "ASSY-1"]
    assert line_rows[-1].reason_code == "ME-CHAIN"
    assert core.st.entities["ASSY-1"].reason == "ME-CHAIN"


def _shift_with_counts(cfg: TwinConfig) -> tuple[EngineCore, list[Effect]]:
    core = EngineCore(cfg, mode="replay")
    events = start_plant(cfg)
    n = 0
    for line in LINES:
        for k in range(100):
            n += 1
            events.append(unit(line, at(2 + k * 4.0), result="defect" if k < 3 else "pass", n=n))
    events += [
        state("ASSY-1", "DOWN_UNPLANNED", at(100), line=True, reason_code="ME-CHAIN"),
        state("CONV-03", "DOWN_UNPLANNED", at(100), reason_code="ME-CHAIN"),
        state("CONV-03", "RUNNING", at(130)),
        state("ASSY-1", "RUNNING", at(130), line=True),
        state("QC-1", "STARVED", at(200), line=True),
        state("QC-1", "RUNNING", at(230), line=True),
    ]
    effects = run(core, sorted(events, key=lambda e: e.ts))
    core.advance_to(SHIFT_B + timedelta(minutes=1))
    effects += core.drain()[0]
    return core, effects


def test_shift_close_writes_final_kpis_bottleneck_dq_and_rules(cfg: TwinConfig) -> None:
    _core, effects = _shift_with_counts(cfg)
    kpis = {k.line: k for k in of_type(effects, KpiShiftRow)}
    assert set(kpis) == set(LINES)
    assy = kpis["ASSY-1"]
    assert (assy.version, assy.final, assy.shift_code) == (1, True, "A")
    v = assy.values
    assert v["pq"] == 100
    assert v["gq"] == 97
    assert v["adot"] == pytest.approx(30)
    assert v["failures"] == 1
    assert v["oee"] == pytest.approx(v["availability"] * v["effectiveness"] * v["quality_ratio"])
    assert kpis["QC-1"].values["adet"] == pytest.approx(30)
    bn = of_type(effects, BottleneckRow)
    assert {b.line for b in bn} == set(LINES)
    assert sum(b.sole_share + b.shifting_share / 2 for b in bn) == pytest.approx(1.0)
    alerts = latest_alerts(effects)
    assert any(k.startswith("AL-Q1|") for k in alerts)  # 3 % defects in every area
    assert any(k.startswith("AL-O1|") for k in alerts)
    assert all(a.status == "resolved" for a in alerts.values() if a.rule_id != "AL-S1")


def test_late_unit_and_reclassification_create_new_versions(cfg: TwinConfig) -> None:
    core, _ = _shift_with_counts(cfg)
    late = run(core, [unit("WELD-1", at(470), n=9999)])
    weld = [k for k in of_type(late, KpiShiftRow) if k.line == "WELD-1"]
    assert weld[-1].version == 2
    assert weld[-1].values["pq"] == 101
    assert of_type(late, AuditRow)
    adot_before = next(k for k in of_type(late, KpiShiftRow) if k.line == "ASSY-1").values["adot"]
    classify = ev(
        "operator",
        "line",
        "ASSY-1",
        {
            "action": "classify_downtime",
            "user": "master1",
            "payload": {
                "entity": "CONV-03",
                "start_ts": at(100).isoformat(),
                "reason_code": "PM-CLEANING",
            },
        },
        SHIFT_B + timedelta(minutes=5),
        source="operator",
    )
    effects = run(core, [classify])
    rows = {d.entity: d for d in of_type(effects, DowntimeRow)}
    assert rows["CONV-03"].reason_source == "operator"
    assert rows["ASSY-1"].planned  # propagated to the line stop of the same instant
    assy = [k for k in of_type(effects, KpiShiftRow) if k.line == "ASSY-1"][-1]
    assert assy.version == 3
    assert assy.values["adot"] == pytest.approx(adot_before - 30)
    assert assy.values["pdot"] == pytest.approx(30)
    audit = of_type(effects, AuditRow)[-1]
    assert audit.user == "master1"
    assert audit.after is not None
    assert audit.after["reason"] == "classify_downtime"


def test_s1_class_a_immediate_microstop_resolved_class_b_after_threshold(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="live")
    run(core, start_plant(cfg))
    effects = run(core, [state("CONV-03", "DOWN_UNPLANNED", at(10), alarm_code="ME-CHAIN")])
    s1 = latest_alerts(effects)
    key = f"AL-S1|CONV-03|{at(10).isoformat().replace('+00:00', 'Z')}"
    assert s1[key].severity == "critical"
    assert s1[key].status == "open"
    effects = run(core, [state("CONV-03", "RUNNING", at(12))])
    resolved = latest_alerts(effects)[key]
    assert resolved.status == "resolved"
    assert resolved.value["microstop"] is True
    effects = run(core, [state("ABB-01", "DOWN_UNPLANNED", at(20), alarm_code="RB-TOOL")])
    assert not [a for a in latest_alerts(effects).values() if a.entity == "ABB-01"]
    core.advance_to(at(26))
    b = [a for a in of_type(core.drain()[0], AlertUpsert) if a.entity == "ABB-01"]
    assert b[-1].severity == "warning"


def test_escalation_in_plant_time_and_long_stop_impact(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="live")
    run(core, start_plant(cfg))
    run(core, [state("CONV-03", "DOWN_UNPLANNED", at(10), alarm_code="ME-CHAIN")])
    core.advance_to(at(21))
    first = of_type(core.drain()[0], AlertEscalate)
    assert [(e.level, e.roles) for e in first] == [(1, ("maintenance",))]
    core.advance_to(at(31))
    effects = core.drain()[0]
    assert [(e.level, e.roles) for e in of_type(effects, AlertEscalate)] == [(2, ("director",))]
    s1 = [a for a in of_type(effects, AlertUpsert) if a.rule_id == "AL-S1"]
    assert s1[-1].value["impact"]["lost_units"] == pytest.approx(21 * 60 / 233, abs=0.05)
    core.advance_to(at(60))
    assert not of_type(core.drain()[0], AlertEscalate)  # end of the chain


def test_d1_daily_limit_and_b1_debounce(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="live")
    run(core, start_plant(cfg))
    run(core, [state("CONV-03", "DOWN_UNPLANNED", at(10), reason_code="ME-CHAIN")])
    core.advance_to(at(57))
    d1 = [a for a in of_type(core.drain()[0], AlertUpsert) if a.rule_id == "AL-D1"]
    assert d1[-1].severity == "warning"
    core.advance_to(at(71))
    d1 = [a for a in of_type(core.drain()[0], AlertUpsert) if a.rule_id == "AL-D1"]
    assert d1[-1].severity == "critical"
    effects = run(core, [buffer("PBS", 0, at(100))])
    assert not [a for a in of_type(effects, AlertUpsert) if a.rule_id == "AL-B1"]
    core.advance_to(at(106))
    b1 = [a for a in of_type(core.drain()[0], AlertUpsert) if a.rule_id == "AL-B1"]
    assert b1[-1].severity == "critical"
    run(core, [buffer("PBS", 15, at(110))])
    core.advance_to(at(116))
    b1 = [a for a in of_type(core.drain()[0], AlertUpsert) if a.rule_id == "AL-B1"]
    assert b1[-1].status == "resolved"


def test_snapshot_restore_continues_identically(cfg: TwinConfig) -> None:
    events = start_plant(cfg)
    events += [unit("ASSY-1", at(5 + k * 4), n=k) for k in range(40)]
    events += [state("CONV-03", "DOWN_UNPLANNED", at(50), alarm_code="ME-CHAIN")]
    tail = [state("CONV-03", "RUNNING", at(90))] + [
        unit("ASSY-1", at(100 + k * 4), n=100 + k) for k in range(20)
    ]
    a = EngineCore(cfg, mode="replay")
    run(a, events)
    b = EngineCore.restore(cfg, a.snapshot(), mode="replay")
    tail_a = run(a, tail)
    tail_b = run(b, tail)
    a.advance_to(SHIFT_B + timedelta(minutes=1))
    b.advance_to(SHIFT_B + timedelta(minutes=1))
    tail_a += a.drain()[0]
    tail_b += b.drain()[0]
    assert tail_a == tail_b
    assert a.snapshot() == b.snapshot()


def test_live_views_and_kpi_messages(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="live")
    run(core, start_plant(cfg))
    _, _live = core.drain()
    run(core, [unit("ASSY-1", at(5), n=1)])
    msgs = core.live_kpi(at(6))
    kinds = {(m.type, m.store[0] if m.store else None) for m in msgs}
    assert ("kpi", "lines") in kinds
    assert ("kpi", "areas") in kinds
    assert ("kpi", "plant") in kinds
    assert ("bottleneck", "bottleneck") in kinds
    line = next(m for m in msgs if m.type == "kpi" and m.data.get("code") == "ASSY-1")
    assert line.data["pq"] == 1
    assert line.data["final"] is False
    views = core.full_views(at(6))
    assert {m.store[0] for m in views if m.store} >= {"equipment", "lines", "buffers", "bottleneck"}
    assert not core.kpi_due(at(6) + timedelta(seconds=2))
    assert core.kpi_due(at(6) + timedelta(seconds=6))


def test_derived_line_state(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="replay", line_state_source="derive")
    run(core, start_plant(cfg))
    run(core, [state("CONV-02", "DOWN_UNPLANNED", at(10), reason_code="EL-DRIVE")])
    assert core.st.entities["ASSY-1"].state == "DOWN_UNPLANNED"
    assert core.st.entities["ASSY-1"].reason == "EL-DRIVE"
    run(core, [state("CONV-02", "RUNNING", at(20)), buffer("PBS", 0, at(21))])
    assert core.st.entities["ASSY-1"].state == "STARVED"
    run(core, [state("ABB-01", "DOWN_UNPLANNED", at(30), reason_code="RB-TOOL")])
    assert core.st.entities["WELD-1"].state == "DEGRADED"


def test_old_stop_reclassification_goes_to_the_writer(cfg: TwinConfig) -> None:
    from qost_engine.core import ReclassifyRequest

    core = EngineCore(cfg, mode="live")
    run(core, start_plant(cfg))
    effects = run(
        core,
        [
            ev(
                "operator",
                "line",
                "ASSY-1",
                {
                    "action": "classify_downtime",
                    "user": "master1",
                    "payload": {
                        "entity": "CONV-03",
                        "start_ts": "2026-10-01T05:00:00+00:00",
                        "reason_code": "ME-CHAIN",
                    },
                },
                at(30),
                source="operator",
            )
        ],
    )
    requests = of_type(effects, ReclassifyRequest)
    assert len(requests) == 1
    assert requests[0].reason_code == "ME-CHAIN"
    assert requests[0].planned is False
    assert requests[0].user == "master1"
