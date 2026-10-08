"""DbSink (FR-ING-02/03) and engine replay against TimescaleDB (database ``<db>_it_m3db``)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from m3_stack import create_database, drop_database, query, truncate, url_string
from sqlalchemy.engine import URL

from qost_engine.core import EngineCore
from qost_engine.core.effects import DowntimeRow, KpiShiftRow, StateInterval
from qost_engine.replay import run_replay
from qost_engine.writer import coalesce
from qost_sim.backfill import run_backfill
from twin_core.config import TwinConfig
from twin_core.db.sink import DbSink, DqRecord
from twin_core.event_sink import MemorySink
from twin_core.events import AnyEvent, RandomIds, make_event

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db_url() -> Iterator[URL]:
    url = create_database("it_m3db")
    yield url
    drop_database(url)


async def _count(url: URL, table: str) -> int:
    return int((await query(url, f"SELECT count(*) AS n FROM {table}"))[0]["n"])


async def test_sink_is_idempotent_and_gates_facts(db_url: URL, cfg: TwinConfig) -> None:
    await truncate(db_url)
    start = cfg.simulation.clock.demo_start - timedelta(hours=8)
    memory = MemorySink()
    await run_backfill(
        cfg, memory, start=start, end=start + timedelta(hours=8), telemetry_period_s=300
    )
    events: list[AnyEvent] = memory.events
    sink = DbSink(url_string(db_url))
    try:
        first = sum(
            [await sink.write_batch(events[i : i + 500]) for i in range(0, len(events), 500)]
        )
        again = sum(
            [await sink.write_batch(events[i : i + 777]) for i in range(0, len(events), 777)]
        )
        assert first == len(events)
        assert again == 0
        assert await _count(db_url, "event_raw") == len(events)
        defects = sum(1 for e in events if e.kind == "defect")
        assert await _count(db_url, "defect") == defects  # no natural key, still not duplicated
        assert await _count(db_url, "unit_event") == sum(1 for e in events if e.kind == "unit")
        levels = {(e.entity, e.ts) for e in events if e.kind == "buffer_level"}
        assert await _count(db_url, "buffer_level") == len(levels)
        record = DqRecord(
            "DQ-07|tag|x|2026-10-16", start, "DQ-07", "warning", "tag", start.date(), {"value": "x"}
        )
        await sink.write_dq([record])
        await sink.write_dq([record])
        assert await _count(db_url, "dq_issue") == 1
    finally:
        await sink.aclose()


async def test_sink_throughput(db_url: URL) -> None:
    """NFR-01: far above 1000 value changes per second through the write path."""
    await truncate(db_url)
    ids = RandomIds()
    base = time.perf_counter()
    events: list[Any] = []
    from datetime import UTC, datetime

    t0 = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
    for i in range(20_000):
        ts = t0 + timedelta(milliseconds=50 * i)
        events.append(
            make_event(
                "telemetry",
                event_id=ids(ts),
                ts=ts,
                source="opcua",
                site="KST",
                entity_type="equipment",
                entity=("ABB-01", "ABB-02", "CONV-03", "BOOTH-02")[i % 4],
                data={"signal": "motor_current_a", "value": float(i % 37), "unit": "A"},
            )
        )
    built = time.perf_counter() - base
    sink = DbSink(url_string(db_url))
    try:
        began = time.perf_counter()
        for i in range(0, len(events), 500):
            await sink.write_batch(events[i : i + 500])
        rate = len(events) / (time.perf_counter() - began)
    finally:
        await sink.aclose()
    assert await _count(db_url, "telemetry") == len(events)
    assert rate > 5_000, (rate, built)


async def test_replay_from_db_equals_in_memory_core(db_url: URL, cfg: TwinConfig) -> None:
    await truncate(db_url)
    start = cfg.simulation.clock.backfill_from
    end = start + timedelta(days=3)
    memory = MemorySink()
    await run_backfill(cfg, memory, start=start, end=end, telemetry_period_s=600)
    sink = DbSink(url_string(db_url))
    try:
        for i in range(0, len(memory.events), 5000):
            await sink.write_batch(memory.events[i : i + 5000])
    finally:
        await sink.aclose()
    began = time.perf_counter()
    stats = await run_replay(cfg, database_url=url_string(db_url), start=start, end=end)
    seconds = time.perf_counter() - began
    core = EngineCore(cfg, mode="replay")
    effects = []
    for event in memory.events:
        if event.kind != "telemetry":
            core.apply(event)
            effects.extend(core.drain()[0])
    core.advance_to(end)
    effects.extend(core.drain()[0])
    merged = coalesce(effects)
    intervals = {
        (e.entity, e.start, e.end, e.state) for e in merged if isinstance(e, StateInterval)
    }
    db_intervals = await query(
        db_url, "SELECT entity, start_ts, end_ts, state FROM equipment_state"
    )
    assert {
        (r["entity"], r["start_ts"], r["end_ts"], r["state"]) for r in db_intervals
    } == intervals
    stops = {(e.entity, e.start, e.reason_code) for e in merged if isinstance(e, DowntimeRow)}
    db_stops = await query(db_url, "SELECT entity, start_ts, reason_code FROM downtime")
    assert {(r["entity"], r["start_ts"], r["reason_code"]) for r in db_stops} == stops
    kpis = {
        (e.line, e.shift_date, e.shift_code, round(e.values["oee"], 9))
        for e in merged
        if isinstance(e, KpiShiftRow)
    }
    db_kpis = await query(
        db_url, "SELECT line, shift_date, shift_code, oee FROM kpi_shift WHERE source = 'events'"
    )
    assert {
        (r["line"], r["shift_date"], r["shift_code"], round(r["oee"], 9)) for r in db_kpis
    } == kpis
    assert stats.events == sum(1 for e in memory.events if e.kind != "telemetry")
    checkpoints = await query(db_url, "SELECT name, event_ts FROM engine_checkpoint ORDER BY name")
    assert [c["name"] for c in checkpoints] == ["baseline", "live"]
    # re-running the replay replaces, it does not duplicate
    await run_replay(cfg, database_url=url_string(db_url), start=start, end=end)
    assert await _count(db_url, "kpi_shift") == len(kpis)
    assert await _count(db_url, "equipment_state") == len(intervals)
    # 3 days in a few seconds: the 46-day history fits NFR-08 comfortably
    assert seconds < 20


async def test_reclassifying_an_old_stop_recomputes_its_shift(db_url: URL, cfg: TwinConfig) -> None:
    """FR-ENG-03 / FR-KPI-04 for stops the core no longer keeps: stored rows are updated and the
    closed shift gets a new KPI version (same functions as live) with an audit entry."""
    from qost_engine.core.effects import ReclassifyRequest
    from qost_engine.writer import EngineWriter

    rows = await query(
        db_url,
        "SELECT d.entity, d.start_ts, d.shift_date, d.shift_code, d.line FROM downtime d "
        "WHERE d.entity = d.line AND NOT d.planned AND NOT d.microstop AND d.end_ts IS NOT NULL "
        "AND d.shift_code IS NOT NULL ORDER BY d.duration_s DESC LIMIT 1",
    )
    assert rows, "the replayed history has line stops"
    stop = rows[0]
    before = await query(
        db_url,
        "SELECT max(version) AS v FROM kpi_shift "
        "WHERE line = :l AND shift_date = :d AND shift_code = :c",
        l=stop["line"],
        d=stop["shift_date"],
        c=stop["shift_code"],
    )
    core = EngineCore(cfg, mode="live")
    op = make_event(
        "operator",
        event_id=RandomIds()(stop["start_ts"] + timedelta(days=3)),
        ts=stop["start_ts"] + timedelta(days=3),
        source="operator",
        site="KST",
        entity_type="line",
        entity=stop["line"],
        data={
            "action": "classify_downtime",
            "user": "master1",
            "payload": {
                "entity": stop["entity"],
                "start_ts": stop["start_ts"].isoformat(),
                "reason_code": "PM-CLEANING",
            },
        },
    )
    core.apply(op)
    effects = core.drain()[0]
    assert [type(e) for e in effects if isinstance(e, ReclassifyRequest)] == [ReclassifyRequest]
    writer = EngineWriter(url_string(db_url), cfg=cfg)
    try:
        await writer.commit(effects)
    finally:
        await writer.aclose()
    row = (
        await query(
            db_url,
            "SELECT reason_code, reason_source, planned FROM downtime "
            "WHERE entity = :e AND start_ts = :s",
            e=stop["entity"],
            s=stop["start_ts"],
        )
    )[0]
    assert (row["reason_code"], row["reason_source"], row["planned"]) == (
        "PM-CLEANING",
        "operator",
        True,
    )
    after = await query(
        db_url,
        "SELECT version, pdot, adot FROM kpi_shift "
        "WHERE line = :l AND shift_date = :d AND shift_code = :c ORDER BY version",
        l=stop["line"],
        d=stop["shift_date"],
        c=stop["shift_code"],
    )
    assert after[-1]["version"] == before[0]["v"] + 1
    assert after[-1]["pdot"] > after[-2]["pdot"]
    assert after[-1]["adot"] < after[-2]["adot"]
    audit = await query(
        db_url, "SELECT action, after FROM audit_log WHERE action = 'kpi_shift.recompute'"
    )
    assert audit
