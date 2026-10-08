"""M3 additions to twin_core: live alert rules and texts, live DQ-04, ULIDs, live contract,
DB sink row building, engine parameters in rules.yaml."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from twin_core.alert_text import alert_message_ru, alert_title_ru, impact_text
from twin_core.clock import ClockState
from twin_core.config import TwinConfig
from twin_core.db.sink import asyncpg_dsn, build_rows
from twin_core.dq import check_flow_balance_live
from twin_core.event_sink import open_sink, sink_schemes
from twin_core.events import (
    DeterministicIds,
    RandomIds,
    content_ulid,
    make_event,
    ulid_timestamp,
)
from twin_core.live import LiveKeys, build_snapshot, envelope, read_snapshot
from twin_core.rules import AL_BUFFER, AL_CKD_COVERAGE, AL_EQUIPMENT_STOP, AlertEvaluator

TS = datetime(2026, 10, 16, 2, 31, 12, 123000, tzinfo=UTC)
DAY = date(2026, 10, 16)


def test_monotonic_ids_preserve_emission_order_within_a_millisecond() -> None:
    for ids in (RandomIds(), DeterministicIds(7, "t")):
        made = [ids(TS) for _ in range(50)]
        assert made == sorted(made)
        assert len(set(made)) == 50
        assert all(ulid_timestamp(x) == TS.replace(microsecond=123000) for x in made)
    a, b = DeterministicIds(7, "t"), DeterministicIds(7, "t")
    assert [a(TS) for _ in range(5)] == [b(TS) for _ in range(5)]
    later = RandomIds()
    first = later(TS)
    assert later(TS + timedelta(milliseconds=1)) > first


def test_content_ulid_is_stable_and_time_ordered() -> None:
    key = "equipment|CONV-03|state|state|2026-10-16T02:31:12.123000Z|DOWN_UNPLANNED|ME-CHAIN"
    assert content_ulid(TS, key) == content_ulid(TS, key)
    assert content_ulid(TS, key) != content_ulid(TS, key + "x")
    assert content_ulid(TS, "a") < content_ulid(TS + timedelta(milliseconds=2), "a")


def test_live_rules(cfg: TwinConfig) -> None:
    ev = AlertEvaluator(cfg.rules)
    a = ev.equipment_stop(
        equipment="CONV-03",
        criticality="A",
        period_date=DAY,
        started_key="2026-10-16T02:31:12Z",
        elapsed_s=0,
        value={"elapsed_min": 0},
    )
    assert a is not None
    assert a.severity == "critical"
    assert a.dedup_key == "AL-S1|CONV-03|2026-10-16T02:31:12Z"
    assert (
        ev.equipment_stop(
            equipment="ABB-01",
            criticality="B",
            period_date=DAY,
            started_key="x",
            elapsed_s=299,
            value={},
        )
        is None
    )
    b = ev.equipment_stop(
        equipment="ABB-01",
        criticality="B",
        period_date=DAY,
        started_key="x",
        elapsed_s=300,
        value={},
    )
    assert b is not None
    assert b.severity == "warning"
    assert ev.buffer_condition(level=0, capacity=30) == ("critical", "low")
    assert ev.buffer_condition(level=5, capacity=30) == ("warning", "low")
    assert ev.buffer_condition(level=28, capacity=30) == ("warning", "high")
    assert ev.buffer_condition(level=15, capacity=30) is None
    b1 = ev.buffer_level(buffer="PBS", level=28, capacity=30, period_date=DAY, started_key="k")
    assert b1 is not None
    assert b1.rule_id == AL_BUFFER
    assert b1.value["direction"] == "high"  # type: ignore[index]
    l1 = ev.ckd_coverage(product="J7", kits=30, daily_plan=23.8, period_date=DAY, started_key="k")
    assert l1 is not None
    assert l1.rule_id == AL_CKD_COVERAGE
    assert (
        ev.ckd_coverage(product="J7", kits=60, daily_plan=23.8, period_date=DAY, started_key="k")
        is None
    )


def test_alert_texts(cfg: TwinConfig) -> None:
    ev = AlertEvaluator(cfg.rules)
    impact = {
        "lost_units": 14.16,
        "bottleneck": False,
        "irrecoverable_units": 0.0,
        "recover_shifts": 1.5,
    }
    s1 = ev.equipment_stop(
        equipment="CONV-03",
        criticality="A",
        period_date=DAY,
        started_key="2026-10-16T02:31:12Z",
        elapsed_s=3300,
        value={"elapsed_min": 55, "reason_code": "ME-CHAIN", "impact": impact},
    )
    assert s1 is not None
    text = alert_message_ru(s1, cfg)
    assert "Конвейер-03" in text
    assert "обрыв цепи" in text
    assert "16.10.2026 07:31" in text
    assert "14,2 авто" in text
    assert "1,5 смены" in text
    assert alert_title_ru(s1, cfg) == cfg.alert_rules[AL_EQUIPMENT_STOP].name_ru
    assert "безвозвратно" in impact_text({**impact, "bottleneck": True})
    assert "не отыгрывается" in impact_text({**impact, "recover_shifts": None})
    assert "влияния на выпуск нет" in impact_text({"lost_units": 0})
    micro = ev.equipment_stop(
        equipment="CONV-01",
        criticality="A",
        period_date=DAY,
        started_key="2026-10-16T03:00:00Z",
        elapsed_s=100,
        value={"elapsed_min": 2, "reason_code": "ME-JAM", "microstop": True},
    )
    assert micro is not None
    assert "микроостановка" in alert_message_ru(micro, cfg)
    b1 = ev.buffer_level(buffer="PBS", level=0, capacity=30, period_date=DAY, started_key="k")
    assert b1 is not None
    assert "риск голодания" in alert_message_ru(b1, cfg)
    l1 = ev.ckd_coverage(product="J7", kits=30, daily_plan=23.8, period_date=DAY, started_key="k")
    assert l1 is not None
    assert "JAC J7" in alert_message_ru(l1, cfg)


def test_flow_balance_live(cfg: TwinConfig) -> None:
    t = cfg.rules.data_quality
    kw: dict[str, Any] = {
        "upstream": "WELD-1",
        "downstream": "PAINT-1",
        "period_date": DAY,
        "shift": "A",
        "thresholds": t,
    }
    assert (
        check_flow_balance_live(
            upstream_out=110, downstream_in=105, buffer_start=10, buffer_end=15, **kw
        )
        is None
    )
    assert (
        check_flow_balance_live(
            upstream_out=110, downstream_in=105, buffer_start=10, buffer_end=13, **kw
        )
        is None
    )
    info = check_flow_balance_live(
        upstream_out=110, downstream_in=105, buffer_start=10, buffer_end=12, **kw
    )
    assert info is not None
    assert info.severity == "info"
    assert info.details["residual_units"] == 3
    warn = check_flow_balance_live(
        upstream_out=110, downstream_in=95, buffer_start=10, buffer_end=12, **kw
    )
    assert warn is not None
    assert warn.severity == "warning"


def test_engine_params_and_thresholds(cfg: TwinConfig) -> None:
    p = cfg.rules.engine
    assert (p.kpi_tick_s, p.bottleneck_window_h, p.buffer_balance_window_min) == (5, 4, 30)
    assert p.unclassified_after_min == 10
    assert p.s1_resolve_microstops
    assert p.dq_live_lost == "downtime"
    assert cfg.rules.thresholds.ckd_coverage_min_days == 2


def test_sink_registry_and_rows(cfg: TwinConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "db" in sink_schemes()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="db sink needs a URL"):
        open_sink("db:")
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
    ids = RandomIds()

    def ev(
        kind: str, entity_type: str, entity: str, data: dict[str, Any], ts: datetime = TS
    ) -> Any:
        maker: Any = make_event
        return maker(
            kind,
            event_id=ids(ts),
            ts=ts,
            source="opcua",
            site="KST",
            entity_type=entity_type,
            entity=entity,
            data=data,
        )

    events = [
        ev(
            "telemetry",
            "equipment",
            "CONV-03",
            {"signal": "vibration_mm_s", "value": 2.5, "unit": "мм/с"},
        ),
        ev(
            "unit",
            "line",
            "ASSY-1",
            {"line": "ASSY-1", "body_id": "B2610160001", "product": "ONIX", "result": "pass"},
        ),
        ev("buffer_level", "buffer", "PBS", {"buffer": "PBS", "level": 5, "capacity": 30}),
        ev("buffer_level", "buffer", "PBS", {"buffer": "PBS", "level": 6, "capacity": 30}),
        ev("ckd", "product", "J7", {"product": "J7", "kits": 30, "event": "consume"}),
        ev(
            "defect",
            "line",
            "PAINT-1",
            {"line": "PAINT-1", "defect_code": "PNT-RUN", "qty": 1, "disposition": "rework"},
        ),
        ev("state", "equipment", "CONV-03", {"state": "DOWN_UNPLANNED", "alarm_code": "ME-CHAIN"}),
    ]
    rows = build_rows(events, {line: cfg.area_of_line(line).code for line in cfg.lines})
    assert len(rows.raw[0]) == 7
    assert json.loads(rows.raw[7][0])["signal"] == "vibration_mm_s"
    assert len(rows.telemetry) == 1
    assert len(rows.units) == 1
    assert len(rows.defects) == 1
    assert next(iter(rows.buffers.values()))[1] == 6  # same buffer and instant: last value wins
    assert rows.defects[0][1][2] == "PAINT"


async def test_live_snapshot_contract(cfg: TwinConfig) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.h: dict[str, dict[str, str]] = {
                "live:equipment": {
                    "CONV-03": json.dumps(
                        {
                            "code": "CONV-03",
                            "state": "DOWN_UNPLANNED",
                            "since": "x",
                            "reason_code": "ME-CHAIN",
                            "alarm": True,
                        }
                    ),
                    "ABB-01": json.dumps({"code": "ABB-01", "state": "RUNNING"}),
                },
                "live:lines": {
                    "ASSY-1": json.dumps(
                        {
                            "code": "ASSY-1",
                            "state": "DOWN_UNPLANNED",
                            "pq": 37,
                            "gq": 36,
                            "oee": 0.86,
                        }
                    )
                },
                "live:buffers": {
                    "PBS": json.dumps(
                        {"code": "PBS", "level": 22, "capacity": 30, "minutes_to_full": 64}
                    )
                },
            }
            self.s = {
                "live:bottleneck": json.dumps(
                    {
                        "current": "PAINT-1",
                        "since": "y",
                        "shift_shares": {"PAINT-1": {"sole": 0.42, "shifting": 0.18}},
                    }
                ),
                "live:alerts_open": json.dumps({"critical": 1, "warning": 3, "info": 2}),
            }

        async def hgetall(self, name: str) -> dict[bytes, bytes]:
            return {k.encode(): v.encode() for k, v in self.h.get(name, {}).items()}

        async def get(self, name: str) -> bytes | None:
            v = self.s.get(name)
            return v.encode() if v else None

    clock = ClockState(TS, TS, 60.0, paused=False, sim_mode="live")
    snap = await read_snapshot(FakeRedis(), cfg, clock=clock)
    assert set(snap) == {"clock", "lines", "equipment", "buffers", "bottleneck", "alerts_open"}
    assert snap["clock"]["shift"] == {"date": "2026-10-16", "code": "A", "elapsed_min": 31.2}
    assert snap["clock"]["plant_time"].startswith("2026-10-16T07:31:12")
    assert [e["code"] for e in snap["equipment"]] == ["ABB-01", "CONV-03"]  # plant.yaml order
    assert snap["equipment"][1]["alarm"] is True
    assert snap["lines"][0]["oee"] == 0.86
    assert snap["bottleneck"]["current"] == "PAINT-1"
    assert snap["alerts_open"]["critical"] == 1
    assert build_snapshot({}, cfg, None)["alerts_open"] == {"critical": 0, "warning": 0, "info": 0}
    msg = json.loads(envelope("state", TS, {"code": "CONV-03"}))
    assert msg == {
        "type": "state",
        "ts": "2026-10-16T02:31:12.123000Z",
        "data": {"code": "CONV-03"},
    }
    keys = LiveKeys("t:live:", "t:live")
    assert keys.key("lines") == "t:live:lines"
    assert len(keys.all) == 9
