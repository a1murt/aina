"""PdM serving in the engine (SPEC §11.1–11.3, M7b): cadence, prediction rows, health in the live
view, AL-M1 thresholds and hysteresis, AL-M2 on the S2 numbers, AL-Q2 (Western Electric)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from engine_support import SHIFT_A, SHIFT_B, at, run, start_plant, state

from qost_engine.core import AlertUpsert, EngineCore, PredictionRow
from qost_engine.core.pdm import LimitItem, PdmTick, UnitPrediction
from qost_engine.core.state import ShiftAcc
from qost_engine.pdm import PdmCache, PdmServing, from_us, to_us
from qost_ml.explain import Factor
from qost_ml.spec import PdmSpec
from twin_core.config import TwinConfig

from engine_support import of_type  # isort: skip


class FakePredictor:
    """Stands in for ``qost_ml.predictor.Predictor``: one probability for every unit."""

    def __init__(self, cfg: TwinConfig, p: float = 0.2) -> None:
        self.spec = PdmSpec.from_config(cfg)
        self.p = p
        self.calls: list[tuple[str, dict[str, float]]] = []

    def predict(self, equipment_type: str, features: Any, *, explain: bool = True) -> Any:
        self.calls.append((equipment_type, dict(features)))
        factor = Factor(
            "vibration_mm_s_slope_4h", 0.3, 1.2, "вибрация растёт: +0.30 мм/с за 4 ч", "k"
        )
        return SimpleNamespace(
            p_failure=self.p,
            health_index=100 * (1 - self.p),
            horizon_h=self.spec.horizon_h,
            model_version="fake-v1",
            source="model",
            top_factors=(factor,),
        )


def unit(code: str, p: float, version: str = "fake-v1") -> UnitPrediction:
    factor = {
        "feature": "x",
        "value": 1.0,
        "shap": 1.0,
        "text_ru": "рост ошибок датчиков",
        "text_kk": "k",
    }
    return UnitPrediction(code, p, 100 * (1 - p), 8.0, version, "model", (factor,))


def started(cfg: TwinConfig) -> EngineCore:
    core = EngineCore(cfg, mode="live")
    run(core, start_plant(cfg))
    return core


def alerts(effects: list[Any], rule: str) -> list[AlertUpsert]:
    return [e for e in of_type(effects, AlertUpsert) if e.rule_id == rule]


# --------------------------------------------------------------------------- cadence


def test_ticks_every_15_plant_minutes_after_the_settle_time(cfg: TwinConfig) -> None:
    core = started(cfg)
    assert core.params.pdm_tick_min == 15
    # a fresh start serves the current slot only (not the one before demo_start)
    assert core.pdm_slot(at(1), 120) is None  # 02:01: the 02:00 slot has not settled yet
    first = core.pdm_slot(at(3), 120)  # the 02:00 slot is served from 02:02
    assert first == at(0)
    core.apply_pdm(PdmTick(first, ()))
    assert core.pdm_slot(at(16), 120) is None  # the same slot is not served twice
    assert core.pdm_slot(at(31), 120) == at(15)  # served late: every slot has its own tick
    core.apply_pdm(PdmTick(at(15), ()))
    assert core.pdm_slot(at(31), 120) is None  # 02:30 settles at 02:32
    assert core.pdm_slot(at(32), 120) == at(30)
    core.apply_pdm(PdmTick(at(30), ()))
    # a long pause serves only the newest slot, not every missed one
    assert core.pdm_slot(at(200), 120) == at(195)
    # slots are aligned to the clock, not to the engine start
    assert core.pdm_slot(at(47), 120) == at(45)


def test_serving_with_a_fake_predictor_covers_every_pdm_unit(cfg: TwinConfig) -> None:
    fake = FakePredictor(cfg, 0.2)
    serving = PdmServing(cfg, fake)
    tick = serving.evaluate(PdmCache(), at(30))
    spec = fake.spec
    assert {u.equipment for u in tick.units} == set(spec.equipment)
    assert {c for c, _ in fake.calls} == {"robot", "conveyor", "fixture", "oven"}
    one = tick.units[0]
    assert one.p_failure == pytest.approx(0.2)
    assert one.health_index == pytest.approx(80.0)
    assert one.factors[0]["text_ru"].startswith("вибрация растёт")
    assert one.model_version == "fake-v1"
    assert one.horizon_h == 8
    for _type, row in fake.calls:  # rule 5: the hidden wear state is not a feature
        assert not any("degradation" in name.lower() for name in row)
    assert tick.lookahead_h == cfg.rules.thresholds.telemetry_limit_lookahead_h


# --------------------------------------------------------------------------- AL-M1


def test_prediction_rows_health_and_al_m1_thresholds(cfg: TwinConfig) -> None:
    core = started(cfg)
    t = cfg.rules.thresholds
    assert (t.pdm_warn_p, t.pdm_crit_p) == (0.5, 0.8)

    def tick(minutes: int, p: float) -> list[Any]:
        core.apply_pdm(PdmTick(at(minutes), (unit("ABB-04", p),)))
        return core.drain()[0]

    out = tick(30, 0.3)
    rows = of_type(out, PredictionRow)
    assert len(rows) == 1
    assert rows[0].equipment == "ABB-04"
    assert rows[0].p_failure == 0.3
    assert rows[0].health_index == 70.0
    assert rows[0].top_factors[0]["text_ru"] == "рост ошибок датчиков"
    assert not alerts(out, "AL-M1")
    assert core.st.health["ABB-04"]["health_index"] == 70.0

    out = tick(45, 0.55)
    (warn,) = alerts(out, "AL-M1")
    assert (warn.severity, warn.status, warn.entity) == ("warning", "open", "ABB-04")
    assert "ABB-04" in warn.message_ru
    assert "55" in warn.message_ru
    assert "8 ч" in warn.message_ru
    assert "рост ошибок датчиков" in warn.message_ru
    assert warn.value["p_failure"] == 0.55
    assert warn.value["horizon_h"] == 8.0
    key = warn.dedup_key
    assert key.startswith("AL-M1|ABB-04|2026-10-16T02:45")

    out = tick(60, 0.56)  # a small move does not rewrite the alert
    assert not alerts(out, "AL-M1")
    out = tick(75, 0.85)
    (crit,) = alerts(out, "AL-M1")
    assert crit.severity == "critical"
    assert crit.dedup_key == key
    out = tick(90, 0.45)  # hysteresis: still above 0.8 x warn_p
    assert not alerts(out, "AL-M1")
    out = tick(105, 0.35)
    (done,) = alerts(out, "AL-M1")
    assert done.status == "resolved"
    assert done.dedup_key == key
    # live: the equipment view carries the health index (snapshot health_index)
    view = core.views["equipment"]["ABB-04"]
    assert view["health_index"] == 65.0


def test_unit_in_service_or_failed_is_not_predicted_and_its_alert_ends(cfg: TwinConfig) -> None:
    core = started(cfg)
    core.apply_pdm(PdmTick(at(30), (unit("ABB-04", 0.9),)))
    assert len(alerts(core.drain()[0], "AL-M1")) == 1
    run(core, [state("ABB-04", "DOWN_UNPLANNED", at(40), alarm_code="RB-TOOL")])
    core.advance_to(at(40) + timedelta(seconds=20))  # a microstop so far: the alert stays
    core.apply_pdm(PdmTick(at(41), (unit("ABB-04", 0.9),)))
    out = core.drain()[0]
    assert not of_type(out, PredictionRow)  # a unit that is down is not predicted
    assert not [a for a in alerts(out, "AL-M1") if a.status == "resolved"]
    core.advance_to(at(48))  # the failure lasts longer than the microstop threshold
    core.apply_pdm(PdmTick(at(48), (unit("ABB-04", 0.9),)))
    resolved = [a for a in alerts(core.drain()[0], "AL-M1") if a.status == "resolved"]
    assert len(resolved) == 1


def test_pdm_state_survives_a_restart(cfg: TwinConfig) -> None:
    core = started(cfg)
    core.apply_pdm(PdmTick(at(30), (unit("ABB-04", 0.62),)))
    core.drain()
    restored = EngineCore.restore(cfg, core.snapshot(), mode="live")
    assert restored.st.pdm_last == core.st.pdm_last
    assert restored.st.health == core.st.health
    assert restored.pdm_slot(at(31), 120) is None
    assert len(restored.st.alerts) == len(core.st.alerts) == 1
    views = {m.data["code"]: m for m in restored.full_views(at(31)) if m.type == "state"}
    assert views["ABB-04"].data["health_index"] == 38.0


# --------------------------------------------------------------------------- AL-M2 (S2)


def _s2_cache(cfg: TwinConfig, rate: float = 7.0) -> tuple[PdmCache, datetime]:
    """BOOTH-02 on 16.10 07:30 local: last shift ramped to 450 and idled, then S2 set 370."""
    rng = random.Random(11)
    demo = cfg.simulation.clock.demo_start  # 07:00 local = 02:00 UTC
    pts: list[tuple[datetime, float]] = []
    t = demo - timedelta(hours=16)  # 15.10 10:00 UTC = 15:00 local, shift B
    level = 394.0  # 7 Pa/h x 8 h of shift B reaches the limit at its end
    while t < demo - timedelta(minutes=5):
        working = t < demo - timedelta(hours=8)  # shift B ended 23:00 local = 18:00 UTC
        if working:
            level = min(450.0, level + rate * 5 / 60)
        pts.append((t, level + rng.gauss(0, 4)))
        t += timedelta(minutes=5)
    now = demo + timedelta(minutes=30)
    t = demo
    while t <= now:
        pts.append((t, 370.0 + rate * (t - demo).total_seconds() / 3600 + rng.gauss(0, 4)))
        t += timedelta(minutes=1)
    cache = PdmCache()
    cache.series[("BOOTH-02", "filter_dp_pa")] = (
        np.array([to_us(p[0]) for p in pts], dtype=np.int64),
        np.array([p[1] for p in pts]),
    )
    return cache, now


def test_al_m2_for_s2_recommends_the_15_00_shift_change(cfg: TwinConfig) -> None:
    cache, now = _s2_cache(cfg)
    serving = PdmServing(cfg, FakePredictor(cfg))
    tick = serving.evaluate(cache, now)
    (item,) = [i for i in tick.limits if i.equipment == "BOOTH-02"]
    assert item.signal == "filter_dp_pa"
    assert item.limit == 450
    assert item.alert
    assert 9.5 <= item.hours_to_limit <= 12.0  # type: ignore[operator]  # ≈ 11.4 h ± noise
    assert item.level_now == pytest.approx(373.5, abs=5)
    assert item.window is not None
    assert item.window.astimezone(cfg.timezone).strftime("%H:%M") == "15:00"
    assert item.saving_min == 40.0
    assert item.saving_cars == pytest.approx(40 * 60 / 233, abs=0.1)

    core = started(cfg)
    core.apply_pdm(tick)
    (m2,) = alerts(core.drain()[0], "AL-M2")
    assert (m2.severity, m2.entity, m2.status) == ("warning", "BOOTH-02", "open")
    assert m2.dedup_key.startswith("AL-M2|BOOTH-02|2026-10-16T02:30")
    assert m2.dedup_key.endswith("/filter_dp_pa")
    assert "в пересменку 15:00" in m2.message_ru
    assert "450" in m2.message_ru
    assert "экономия ≈ 40 мин" in m2.message_ru
    # an unchanged forecast is not rewritten, a replacement (planned service) closes the alert
    core.apply_pdm(PdmTick(at(45), (), tick.limits, tick.lookahead_h))
    assert not alerts(core.drain()[0], "AL-M2")
    run(core, [state("BOOTH-02", "DOWN_PLANNED", at(60), alarm_code="MT-FILTER")])
    core.apply_pdm(PdmTick(at(60), (), tick.limits, tick.lookahead_h))
    (closed,) = alerts(core.drain()[0], "AL-M2")
    assert closed.status == "resolved"


def test_al_m2_waits_for_a_rising_trend_and_clears_with_margin(cfg: TwinConfig) -> None:
    cache, now = _s2_cache(cfg, rate=2.0)  # 80 Pa / 2 Pa/h = 40 h: no alert
    serving = PdmServing(cfg, FakePredictor(cfg))
    (item,) = [i for i in serving.evaluate(cache, now).limits if i.equipment == "BOOTH-02"]
    assert not item.alert
    core = started(cfg)

    def limit(hours: float, alert: bool) -> LimitItem:
        return LimitItem(
            "BOOTH-02",
            "filter_dp_pa",
            "Перепад давления на фильтрах",
            "Па",
            450.0,
            400.0,
            7.0,
            hours,
            None,
            None,
            40.0,
            10.3,
            alert,
        )

    near = limit(11.0, True)
    core.apply_pdm(PdmTick(at(30), (), (near,), 12.0))
    assert len(alerts(core.drain()[0], "AL-M2")) == 1
    inside_margin = limit(12.6, False)  # ≤ 12 + 1 h: stays open
    core.apply_pdm(PdmTick(at(45), (), (inside_margin,), 12.0))
    assert not alerts(core.drain()[0], "AL-M2")
    gone = limit(20.0, False)
    core.apply_pdm(PdmTick(at(60), (), (gone,), 12.0))
    assert [a.status for a in alerts(core.drain()[0], "AL-M2")] == ["resolved"]


def test_time_helpers_round_trip() -> None:
    moment = datetime(2026, 10, 16, 2, 0, 0, 123456, tzinfo=UTC)
    assert from_us(to_us(moment)) == moment


# --------------------------------------------------------------------------- AL-Q2


def _close(core: EngineCore, area: str, n: int, defects: int, day: int, code: str) -> list[Any]:
    acc = ShiftAcc(f"2026-10-{day:02d}", code, 0.0, 0.0)
    kpi = SimpleNamespace(pq=n, gq=n - defects)
    core._spc(acc, {area: kpi}, 1.0, first=True)  # type: ignore[dict-item]
    return core.drain()[0]


def test_al_q2_raises_on_a_point_beyond_3_sigma_and_on_a_run(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="live")
    rng = random.Random(5)
    quiet: list[Any] = []
    for k in range(20):  # in control around 3 %, n ≈ 110
        quiet += _close(core, "PAINT", 110, 3 + rng.choice((-1, 0, 0, 1)), 1 + k // 2, "AB"[k % 2])
    assert not alerts(quiet, "AL-Q2")
    out = _close(core, "PAINT", 110, 15, 12, "A")  # 13.6 % ≫ UCL
    (q2,) = alerts(out, "AL-Q2")
    assert (q2.severity, q2.entity, q2.status) == ("warning", "PAINT", "open")
    assert q2.dedup_key == "AL-Q2|PAINT|2026-10-12/A"
    assert 1 in q2.value["rules"]
    assert q2.value["side"] == 1
    assert "правило 1" in q2.message_ru
    assert "вне статистического контроля" in q2.message_ru
    # the chart keeps the last N shifts per area; a recompute (first=False) never re-alerts
    assert len(core.st.spc["PAINT"]) == 21
    again = ShiftAcc("2026-10-12", "A", 0.0, 0.0)
    core._spc(again, {"PAINT": SimpleNamespace(pq=110, gq=95)}, 2.0, first=False)  # type: ignore[dict-item]
    assert not alerts(core.drain()[0], "AL-Q2")
    assert len(core.st.spc["PAINT"]) == 21
    # rule 4: eight shifts in a row above the centre line (each only slightly)
    core2 = EngineCore(cfg, mode="live")
    for k in range(20):
        _close(core2, "WELD", 200, 4 if k % 2 else 2, 1 + k // 2, "AB"[k % 2])
    hits: list[Any] = []
    for k in range(8):
        hits += alerts(_close(core2, "WELD", 200, 5, 12 + k // 2, "AB"[k % 2]), "AL-Q2")
    assert hits
    assert 4 in hits[-1].value["rules"]


def test_al_q2_history_is_written_resolved_in_replay(cfg: TwinConfig) -> None:
    core = EngineCore(cfg, mode="replay", resolve_history_alerts=True)
    for k in range(20):
        _close(core, "PAINT", 110, 3, 1 + k // 2, "AB"[k % 2])
    (q2,) = alerts(_close(core, "PAINT", 110, 20, 12, "A"), "AL-Q2")
    assert q2.status == "resolved"
    assert not core.st.alerts


def test_shifts_in_the_fixture_are_a_b(cfg: TwinConfig) -> None:
    assert timedelta(hours=8) == SHIFT_B - SHIFT_A
