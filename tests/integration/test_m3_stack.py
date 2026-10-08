"""M3 acceptance on real processes: T-INT, demo reset, latency p95, T-SF, engine restart.

sim -> OPC UA / MQTT -> collector -> TimescaleDB + Redis Stream -> engine -> live view / pub-sub.
Uses its own database ``<db>_it_m3``, Redis DB 15 with prefixed channels and a unique MQTT root.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from m3_stack import (
    REDIS_URL,
    Stack,
    TcpProxy,
    create_database,
    drop_database,
    query,
    truncate,
    url_string,
)
from sqlalchemy.engine import URL

from qost_engine.core import EngineCore
from qost_engine.core.effects import DowntimeRow, StateInterval
from qost_engine.replay import event_from_row
from qost_engine.writer import coalesce
from support import REPO_ROOT
from twin_core.clock import ClockState
from twin_core.config import TwinConfig
from twin_core.live import LiveKeys, read_snapshot

pytestmark = pytest.mark.integration

RESULTS = REPO_ROOT / "var"


@pytest.fixture(scope="module")
def db_url() -> Iterator[URL]:
    url = create_database("it_m3")
    yield url
    drop_database(url)


@pytest.fixture
async def stack(db_url: URL, tmp_path: Path) -> AsyncIterator[Stack]:
    await truncate(db_url)
    s = Stack(db_url, tmp_path)
    try:
        yield s
    finally:
        await s.stop()


async def _clock(stack: Stack) -> ClockState | None:
    raw = await stack.redis.get("plant:clock")
    return ClockState.from_json(raw) if raw else None


async def test_t_int_s1_and_demo_reset(stack: Stack, cfg: TwinConfig) -> None:
    await stack.start()
    await stack.run_plant(60.0)
    await asyncio.sleep(4.0)
    pubsub = stack.redis.pubsub()
    await pubsub.subscribe(stack.channel)
    began = time.monotonic()
    applied = await stack.sim("POST", "/inject", {"scenario_id": "S1-CHAIN-BREAK"})
    t_inject = datetime.fromisoformat(applied["applied_at"])
    got_delta = got_view = got_db = got_alert = None
    keys = LiveKeys()
    while time.monotonic() - began < 5.0 and None in (got_delta, got_view, got_db, got_alert):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if msg is not None and got_delta is None:
            body = json.loads(msg["data"])
            if (
                body["type"] == "state"
                and body["data"].get("code") == "CONV-03"
                and body["data"].get("state") == "DOWN_UNPLANNED"
            ):
                got_delta = time.monotonic() - began
        if got_view is None:
            raw = await stack.redis.hget(keys.key("equipment"), "CONV-03")
            view = json.loads(raw) if raw else {}
            if view.get("state") == "DOWN_UNPLANNED" and view.get("reason_code") == "ME-CHAIN":
                got_view = time.monotonic() - began
        if got_db is None:
            rows = await query(
                stack.db_url,
                "SELECT reason_code FROM downtime WHERE entity = 'CONV-03' "
                "AND end_ts IS NULL AND start_ts >= :t",
                t=t_inject - timedelta(seconds=1),
            )
            if rows and rows[0]["reason_code"] == "ME-CHAIN":
                got_db = time.monotonic() - began
        if got_alert is None:
            rows = await query(
                stack.db_url,
                "SELECT severity, status FROM alert WHERE rule_id = 'AL-S1' AND entity = 'CONV-03'",
            )
            if any(r["severity"] == "critical" and r["status"] == "open" for r in rows):
                got_alert = time.monotonic() - began
    timings = {"delta": got_delta, "view": got_view, "downtime": got_db, "alert": got_alert}
    assert None not in timings.values(), (timings, stack.log("engine"), stack.log("collector"))
    assert all(v is not None and v <= 5.0 for v in timings.values()), timings
    snapshot = await read_snapshot(stack.redis, cfg, clock=await _clock(stack))
    conv = next(e for e in snapshot["equipment"] if e["code"] == "CONV-03")
    assert conv["state"] == "DOWN_UNPLANNED"
    assert conv["alarm"] is True
    assert snapshot["alerts_open"]["critical"] >= 1
    assert {line["code"] for line in snapshot["lines"]} == set(cfg.flow_lines)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "m3_t_int.json").write_text(json.dumps(timings, indent=2))

    # demo reset: the engine deletes the live tail, restores the baseline and acks
    status = await stack.sim("POST", "/reset", {"to": "demo_start"})
    assert status["cleanup"] == "acked"
    demo_start = cfg.simulation.clock.demo_start
    rows = await query(
        stack.db_url,
        "SELECT count(*) AS n FROM downtime WHERE entity = 'CONV-03' AND start_ts >= :t",
        t=t_inject - timedelta(seconds=1),
    )
    assert rows[0]["n"] == 0
    rows = await query(
        stack.db_url,
        "SELECT count(*) AS n FROM alert WHERE rule_id = 'AL-S1' AND entity = 'CONV-03'",
    )
    assert rows[0]["n"] == 0
    await asyncio.sleep(3.0)
    view = json.loads(await stack.redis.hget(LiveKeys().key("equipment"), "CONV-03") or "{}")
    assert view.get("state") == "RUNNING"
    rows = await query(stack.db_url, "SELECT min(ts) AS t FROM event_raw")
    assert rows[0]["t"] is None or rows[0]["t"] >= demo_start
    await pubsub.aclose()  # type: ignore[no-untyped-call]


# --------------------------------------------------------------------------- latency (NFR-01)


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "n": len(values),
        "p50_s": round(statistics.median(ordered), 3),
        "p95_s": round(p95, 3),
        "max_s": round(ordered[-1], 3),
    }


async def _measure(stack: Stack, seconds: float, warmup: float = 3.0) -> dict[str, list[float]]:
    """OPC UA tag change -> ``live`` delta: receive wall time minus the wall time of the event's
    plant timestamp (from ``plant:clock``: wall_ts + (ts - plant_time) / speed)."""
    from twin_core.clock import system_now

    pubsub = stack.redis.pubsub()
    await pubsub.subscribe(stack.channel)
    out: dict[str, list[float]] = {"opcua": [], "units_mqtt": []}
    clock = await _clock(stack)
    began = time.monotonic()
    last_clock = 0.0
    while time.monotonic() - began < seconds:
        if time.monotonic() - last_clock > 0.2:
            clock = await _clock(stack)
            last_clock = time.monotonic()
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if msg is None or clock is None or clock.paused or time.monotonic() - began < warmup:
            continue
        received = system_now()
        body = json.loads(msg["data"])
        if body["type"] not in ("state", "buffer", "unit"):
            continue
        ts = datetime.fromisoformat(body["ts"].replace("Z", "+00:00"))
        wall = clock.wall_ts + (ts - clock.plant_time) / clock.speed
        latency = (received - wall).total_seconds()
        out["units_mqtt" if body["type"] == "unit" else "opcua"].append(latency)
    await pubsub.aclose()  # type: ignore[no-untyped-call]
    return out


async def test_latency_p95(stack: Stack) -> None:
    await stack.start()
    report: dict[str, Any] = {}
    for speed, seconds in ((60.0, 90.0), (300.0, 45.0)):
        await stack.run_plant(speed)
        measured = await _measure(stack, seconds)
        report[f"{int(speed)}x"] = {k: _summary(v) for k, v in measured.items() if v}
    report["engine"] = {
        k: v
        for k, v in (await stack.stats("engine")).items()
        if k in ("processed", "commits", "lag_ms", "late_events")
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "m3_latency.json").write_text(json.dumps(report, indent=2))
    for label in ("60x", "300x"):
        opcua = report[label]["opcua"]
        assert opcua["n"] > 100, report
        assert opcua["p95_s"] <= 2.0, report


# --------------------------------------------------------------------------- T-SF


async def _docker(action: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        action,
        "qost-timescaledb-1",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await proc.wait() == 0


async def _pg_ready() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "qost-timescaledb-1",
        "pg_isready",
        "-h",
        "127.0.0.1",
        "-U",
        "qost",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def test_t_sf_database_outage(db_url: URL, tmp_path: Path) -> None:
    """T-SF: the database is unavailable for 60 s during live; afterwards every event published
    to the stream is in the database (spool replay, idempotent), the engine catches up."""
    docker = os.environ.get("QOST_T_SF_DOCKER") == "1"
    await truncate(db_url)
    proxy = TcpProxy(db_url.host or "localhost", db_url.port or 5432)
    await proxy.start()
    via = url_string(db_url.set(host="127.0.0.1", port=proxy.port))
    stack = Stack(db_url, tmp_path, collector_db_url=via, engine_db_url=via)
    report: dict[str, Any] = {"mode": "docker stop" if docker else "tcp proxy cut"}
    try:
        await stack.start()
        await stack.run_plant(300.0)
        await asyncio.sleep(10.0)
        began = time.monotonic()
        if docker:
            await _docker("stop")
        else:
            await proxy.cut()
        try:
            await asyncio.sleep(30.0)
            during = await stack.stats("collector")
            engine_during = await stack.stats("engine")
            report["during"] = {
                "db_spool_bytes": during["outputs"]["db"]["spool_bytes"],
                "stream_written": during["outputs"]["stream"]["written"],
                "engine_db_ok": engine_during["db_ok"],
                "engine_pending_ops": engine_during["pending_ops"],
            }
            await asyncio.sleep(max(0.0, 60.0 - (time.monotonic() - began)))
        finally:
            if docker:
                await _docker("start")
                for _ in range(120):
                    if await _pg_ready():
                        break
                    await asyncio.sleep(0.5)
            else:
                await proxy.restore()
        report["outage_s"] = round(time.monotonic() - began, 1)
        assert report["during"]["db_spool_bytes"] > 0
        assert report["during"]["engine_db_ok"] is False
        await asyncio.sleep(10.0)
        await stack.sim("POST", "/pause")
        final = await stack.caught_up(timeout_s=180.0)
        entries: Any = await stack.redis.xrange("events")
        stream_ids = {json.loads(fields[b"j"])["event_id"] for _id, fields in entries}
        rows = await query(db_url, "SELECT event_id FROM event_raw")
        db_ids = {r["event_id"] for r in rows}
        report["published"] = final["collector"]["emitted"]
        report["stream"] = len(stream_ids)
        report["db"] = len(db_ids)
        assert stream_ids == db_ids
        assert len(db_ids) == final["collector"]["emitted"]
        overlaps = await query(
            db_url,
            "SELECT count(*) AS n FROM equipment_state a JOIN equipment_state b "
            "ON a.entity = b.entity AND a.start_ts < b.start_ts "
            "AND (a.end_ts IS NULL OR a.end_ts > b.start_ts)",
        )
        assert overlaps[0]["n"] == 0
        last_entry = entries[-1][0].decode()
        assert final["engine"]["checkpoint_id"] == last_entry
        RESULTS.mkdir(exist_ok=True)
        name = "m3_t_sf_docker.json" if docker else "m3_t_sf.json"
        (RESULTS / name).write_text(json.dumps(report, indent=2, default=str))
    finally:
        await stack.stop()
        await proxy.close()


# --------------------------------------------------------------------------- NFR-03 restart


async def test_engine_restart_recovers_from_checkpoint(stack: Stack, cfg: TwinConfig) -> None:
    import signal as _signal

    await stack.start()
    await stack.run_plant(300.0)
    await asyncio.sleep(20.0)
    stack.kill("engine", _signal.SIGKILL)
    await asyncio.sleep(5.0)
    stack.spawn("engine")
    await stack.wait_http(f"http://127.0.0.1:{stack.ports['engine']}/readyz", 60)
    await asyncio.sleep(15.0)
    await stack.sim("POST", "/pause")
    await stack.caught_up(timeout_s=120.0)
    rows = await query(
        stack.db_url,
        "SELECT event_id, ts, received_ts, source, entity_type, entity, kind, "
        "data::text AS data, quality FROM event_raw WHERE kind <> 'telemetry' "
        "ORDER BY ts, received_ts NULLS FIRST, event_id",
    )
    core = EngineCore(cfg, mode="replay")
    effects = []
    for row in rows:
        core.apply(event_from_row(row, "KST"))
        effects.extend(core.drain()[0])
    cutoff = rows[-1]["ts"] - timedelta(minutes=1)
    replayed = {
        (e.entity, e.start, e.end, e.state, e.reason_code)
        for e in coalesce(effects)
        if isinstance(e, StateInterval) and e.end is not None and e.end < cutoff
    }
    live_rows = await query(
        stack.db_url,
        "SELECT entity, start_ts, end_ts, state, reason_code FROM equipment_state "
        "WHERE end_ts IS NOT NULL AND end_ts < :c",
        c=cutoff,
    )
    live = {
        (r["entity"], r["start_ts"], r["end_ts"], r["state"], r["reason_code"]) for r in live_rows
    }
    assert len(live) > 50
    if live != replayed:
        RESULTS.mkdir(exist_ok=True)
        diff = {
            "live_only": sorted(map(str, live - replayed)),
            "replay_only": sorted(map(str, replayed - live)),
        }
        (RESULTS / "m3_restart_diff.json").write_text(json.dumps(diff, indent=2))
    assert live == replayed
    replayed_stops = {
        (e.entity, e.start, e.end, e.reason_code, e.microstop)
        for e in coalesce(effects)
        if isinstance(e, DowntimeRow) and e.end is not None and e.end < cutoff
    }
    stops = await query(
        stack.db_url,
        "SELECT entity, start_ts, end_ts, reason_code, microstop FROM downtime WHERE end_ts < :c",
        c=cutoff,
    )
    assert {
        (r["entity"], r["start_ts"], r["end_ts"], r["reason_code"], r["microstop"]) for r in stops
    } == replayed_stops
    assert REDIS_URL.endswith("/15")
