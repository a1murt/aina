"""Live contract (SPEC §12.3): ``GET /live/snapshot`` from the engine's real live views, and
``WS /ws/live`` — snapshot on connect, delta forwarding, subscribe filter, throttling, resync."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from engine_support import SHIFT_A, at, run, start_plant, state, unit
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api_support import PROBLEM, FakeRedis, QueueSource, bearer, token
from qost_api.app import create_app
from qost_api.live.hub import Throttle, throttle_key
from qost_engine.core import EngineCore
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig

NOW = at(150)
"""09:30 local, 150 min into shift A of 16.10."""


def engine_views(cfg: TwinConfig) -> FakeRedis:
    """``live:*`` exactly as the engine's publisher stores them after a short live run."""
    core = EngineCore(cfg, mode="live")
    events = start_plant(cfg, SHIFT_A)
    events += [unit("ASSY-1", at(10 + i), n=i) for i in range(30)]
    events += [state("CONV-03", "DOWN_UNPLANNED", at(140), reason_code="ME-CHAIN")]
    events += [state("ASSY-1", "DOWN_UNPLANNED", at(140), line=True)]
    run(core, events)
    core.advance_to(NOW)
    redis = FakeRedis()
    for msg in core.full_views(NOW):
        if msg.store is None:
            continue
        name, field, value = msg.store
        if value is not None:
            redis.put_view(name, field, value)
    redis.put_view("alerts_open", None, {"critical": 1, "warning": 0, "info": 2})
    return redis


@pytest.fixture
def redis(cfg: TwinConfig) -> FakeRedis:
    return engine_views(cfg)


@pytest.fixture
def source() -> QueueSource:
    return QueueSource()


@pytest.fixture
def client(
    cfg: TwinConfig, redis: FakeRedis, source: QueueSource, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("WS_MIN_INTERVAL_MS", "150")
    monkeypatch.setenv("WS_CLOCK_TICK_S", "3600")
    app = create_app(
        cfg,
        clock=ManualClock(NOW),
        database_url=None,
        redis=redis,  # type: ignore[arg-type]
        live_source=source,
    )
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- snapshot


def test_snapshot_contract(client: TestClient, cfg: TwinConfig) -> None:
    response = client.get("/api/v1/live/snapshot", headers=bearer("operator"))
    assert response.status_code == 200, response.text
    snap = response.json()
    assert {"clock", "lines", "equipment", "buffers", "bottleneck", "alerts_open"} <= set(snap)
    clock = snap["clock"]
    assert clock["plant_time"] == "2026-10-16T09:30:00+05:00"
    assert clock["shift"] == {"date": "2026-10-16", "code": "A", "elapsed_min": 150.0}
    assert clock["mode"] in ("sim", "system")
    assert isinstance(clock["speed"], int | float)
    assert [line["code"] for line in snap["lines"]] == list(cfg.flow_lines)
    assy = next(line for line in snap["lines"] if line["code"] == "ASSY-1")
    assert set(assy) >= {
        "code", "state", "since", "pq", "gq", "plan_to_now",
        "availability", "effectiveness", "quality_ratio", "oee",
    }  # fmt: skip
    assert assy["state"] == "DOWN_UNPLANNED"
    assert assy["pq"] == 30
    assert 0 < assy["availability"] < 1
    assert [e["code"] for e in snap["equipment"]] == list(cfg.equipment)
    conv = next(e for e in snap["equipment"] if e["code"] == "CONV-03")
    assert conv["state"] == "DOWN_UNPLANNED"
    assert conv["reason_code"] == "ME-CHAIN"
    assert conv["alarm"] is False or conv["alarm"] is True
    assert "health_index" in conv
    assert [b["code"] for b in snap["buffers"]] == [b.code for b in cfg.plant.buffers]
    assert {"level", "capacity", "minutes_to_full"} <= set(snap["buffers"][0])
    assert set(snap["bottleneck"]) >= {"current", "since", "shift_shares"}
    for shares in snap["bottleneck"]["shift_shares"].values():
        assert set(shares) == {"sole", "shifting"}
    assert snap["alerts_open"] == {"critical": 1, "warning": 0, "info": 2}
    # superset: open stops for the incident list
    assert any(d["entity"] == "CONV-03" for d in snap["downtime_open"])


def test_snapshot_without_redis_is_503(cfg: TwinConfig) -> None:
    with TestClient(create_app(cfg, clock=ManualClock(NOW), database_url=None, redis=None)) as c:
        response = c.get("/api/v1/live/snapshot", headers=bearer("master"))
        assert response.status_code == 503
        assert response.headers["content-type"] == PROBLEM
        assert response.json()["type"] == "/problems/no-redis"


def test_snapshot_with_redis_down_is_503(client: TestClient, redis: FakeRedis) -> None:
    redis.fail = True
    response = client.get("/api/v1/live/snapshot", headers=bearer("master"))
    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM


# --------------------------------------------------------------------------- WebSocket


def _url(role: str = "master") -> str:
    return f"/ws/live?token={token(role, lines=('ASSY-1',) if role == 'operator' else ())}"


def _recv(ws: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(ws.receive_text())
    return data


def test_ws_sends_snapshot_first(client: TestClient) -> None:
    with client.websocket_connect(_url("operator")) as ws:
        first = _recv(ws)
        assert first["type"] == "snapshot"
        assert {"clock", "lines", "equipment", "buffers", "bottleneck", "alerts_open"} <= set(
            first["data"]
        )


def test_ws_rejects_a_bad_token(client: TestClient) -> None:
    with (
        client.websocket_connect("/ws/live?token=nope") as ws,
        pytest.raises(WebSocketDisconnect) as info,
    ):
        ws.receive_text()
    assert info.value.code == 4401
    with client.websocket_connect("/ws/live") as ws, pytest.raises(WebSocketDisconnect) as info:
        ws.receive_text()
    assert info.value.code == 4401


def test_ws_forwards_deltas(client: TestClient, source: QueueSource) -> None:
    with client.websocket_connect(_url()) as ws:
        assert _recv(ws)["type"] == "snapshot"
        source.push("state", {"entity_type": "equipment", "code": "CONV-03", "state": "RUNNING"})
        source.push("alert", {"dedup_key": "AL-S1|CONV-03|x", "severity": "critical"})
        got = [_recv(ws), _recv(ws)]
        assert [m["type"] for m in got] == ["state", "alert"]
        assert got[0]["data"]["state"] == "RUNNING"
        assert got[0]["ts"] == "2026-10-16T04:30:00Z"


def test_ws_subscribe_filter(client: TestClient, source: QueueSource) -> None:
    with client.websocket_connect(_url()) as ws:
        assert _recv(ws)["type"] == "snapshot"
        ws.send_text(json.dumps({"subscribe": ["alert"]}))
        ack = _recv(ws)
        assert ack == {**ack, "type": "subscribed", "data": {"types": ["alert"]}}
        source.push("kpi", {"level": "line", "code": "ASSY-1", "oee": 0.8})
        source.push("state", {"code": "CONV-03", "state": "RUNNING"})
        source.push("alert", {"dedup_key": "k1", "severity": "warning"})
        msg = _recv(ws)
        assert msg["type"] == "alert"
        ws.send_text(json.dumps({"subscribe": ["nope"]}))
        assert _recv(ws)["type"] == "error"
        ws.send_text(json.dumps({"ping": 1}))
        assert _recv(ws)["type"] == "pong"


def test_ws_throttles_per_type_and_entity(client: TestClient, source: QueueSource) -> None:
    with client.websocket_connect(_url()) as ws:
        assert _recv(ws)["type"] == "snapshot"
        for i in range(10):
            source.push("kpi", {"level": "line", "code": "ASSY-1", "pq": i})
        source.push("kpi", {"level": "line", "code": "WELD-1", "pq": 99})
        started = time.monotonic()
        got = [_recv(ws) for _ in range(3)]
        elapsed = time.monotonic() - started
        assy = [m["data"]["pq"] for m in got if m["data"]["code"] == "ASSY-1"]
        weld = [m["data"]["pq"] for m in got if m["data"]["code"] == "WELD-1"]
        assert assy == [0, 9]  # first at once, the latest at the end of the interval
        assert weld == [99]  # another entity is not held back
        assert elapsed >= 0.1
        source.push("alert", {"dedup_key": "sentinel"})
        assert _recv(ws)["data"]["dedup_key"] == "sentinel"  # nothing else was queued


def test_ws_units_are_never_coalesced(client: TestClient, source: QueueSource) -> None:
    with client.websocket_connect(_url()) as ws:
        assert _recv(ws)["type"] == "snapshot"
        for i in range(5):
            source.push("unit", {"line": "QC-1", "body_id": f"B{i}"}, ts=f"2026-10-16T04:30:0{i}Z")
        assert [_recv(ws)["data"]["body_id"] for _ in range(5)] == [f"B{i}" for i in range(5)]


def test_ws_resends_snapshot_when_the_engine_rebuilds(
    client: TestClient, source: QueueSource, redis: FakeRedis
) -> None:
    with client.websocket_connect(_url()) as ws:
        assert _recv(ws)["type"] == "snapshot"
        redis.put_view("alerts_open", None, {"critical": 0, "warning": 0, "info": 0})
        source.push("snapshot", {"reason": "reset"})
        again = _recv(ws)
        assert again["type"] == "snapshot"
        assert again["data"]["alerts_open"]["critical"] == 0
        ws.send_text(json.dumps({"resync": True}))
        assert _recv(ws)["type"] == "snapshot"


def test_ws_alias_under_api_prefix(client: TestClient) -> None:
    with client.websocket_connect(f"/api/v1/ws/live?token={token('director')}") as ws:
        assert _recv(ws)["type"] == "snapshot"


def test_clock_tick_message(cfg: TwinConfig, redis: FakeRedis, source: QueueSource) -> None:
    app = create_app(
        cfg,
        clock=ManualClock(NOW, speed=60.0),
        database_url=None,
        redis=redis,  # type: ignore[arg-type]
        live_source=source,
    )
    with TestClient(app) as c, c.websocket_connect(_url()) as ws:
        assert _recv(ws)["type"] == "snapshot"
        msg = _recv(ws)  # first tick goes out when a client is connected
        assert msg["type"] == "clock"
        assert msg["data"]["event"] == "tick"
        assert msg["data"]["plant_time"] == "2026-10-16T09:30:00+05:00"
        assert msg["data"]["shift"]["code"] == "A"


# --------------------------------------------------------------------------- throttle (pure)


def test_throttle_keeps_first_and_latest() -> None:
    t = Throttle(0.5)
    key = ("kpi", "line", "ASSY-1")
    assert t.offer(key, "a", 0.0) == "a"
    assert t.offer(key, "b", 0.1) is None
    assert t.offer(key, "c", 0.2) is None
    assert t.next_due() == 0.5
    assert t.due(0.4) == []
    assert t.due(0.5) == ["c"]
    assert t.offer(key, "d", 0.6) is None  # 0.1 s after the trailing message
    assert t.due(1.0) == ["d"]
    assert t.offer(key, "e", 2.0) == "e"


def test_throttle_keys() -> None:
    assert throttle_key({"type": "state", "data": {"code": "CONV-03"}}) == ("state", "CONV-03")
    assert throttle_key({"type": "state", "data": {"downtime": {"entity": "CONV-03"}}}) == (
        "downtime",
        "CONV-03",
    )
    assert throttle_key({"type": "kpi", "data": {"level": "area", "code": "WELD"}}) == (
        "kpi",
        "area",
        "WELD",
    )
    assert throttle_key({"type": "bottleneck", "data": {}}) == ("bottleneck",)
    assert throttle_key({"type": "unit", "ts": "t", "data": {"line": "L", "body_id": "B"}})[0] == (
        "unit"
    )
    assert timedelta(seconds=0) == timedelta(0)
