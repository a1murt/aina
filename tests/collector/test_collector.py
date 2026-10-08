"""Collector pipeline without infrastructure: normalizer, fidelity vs the simulator, spool,
spooled outputs and batching (SPEC §7.1–7.3, FR-ING-01..04)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from qost_collector.normalize import Normalizer, mqtt_payload
from qost_collector.outputs import Batch, SpooledOutput
from qost_collector.spool import Spool
from qost_collector.tagmap import Binding, Bindings, compile_bindings
from qost_sim.address_space import build_address_space, load_contract, state_codes
from qost_sim.model import EventFactory, PlantModel, Rec
from qost_sim.opcua_server import OpcUaServer
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState
from twin_core.events import AnyEvent, RandomIds, make_event

TS = datetime(2026, 10, 16, 2, 30, tzinfo=UTC)
RECEIVED = TS + timedelta(seconds=1)


def data(event: AnyEvent) -> Any:
    """The payload of an event, untyped (tests read kind-specific fields)."""
    return event.data


def norm(cfg: TwinConfig, source: str = "opcua") -> tuple[Normalizer, Bindings]:
    assert cfg.tag_map is not None
    return Normalizer(cfg, dict(cfg.tag_map.opcua.state_enum), source), compile_bindings(  # type: ignore[arg-type]
        cfg, cfg.tag_map
    )


def node(bindings: Bindings, path: str) -> Binding:
    return bindings.nodes[f"ns=2;s=KST.{path}"]


def test_state_and_alarm_code_join_and_reasons(cfg_with_map: TwinConfig) -> None:
    n, b = norm(cfg_with_map)
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.State"), 4, TS)
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.AlarmCode"), "ME-CHAIN", TS)
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.Alarm"), True, TS)
    n.feed(node(b, "ASSY.ASSY-1.State"), 4, TS)
    events = n.flush(RECEIVED)
    assert [e.kind for e in events] == ["state", "alarm", "state"]
    conv = events[0]
    assert data(conv).state == EquipmentState.DOWN_UNPLANNED
    assert data(conv).alarm_code == "ME-CHAIN"
    assert conv.received_ts == RECEIVED
    assert data(events[1]).code == "ME-CHAIN"
    assert events[2].entity_type == "line"


def test_late_alarm_code_and_new_cause_restate_the_state(cfg_with_map: TwinConfig) -> None:
    n, b = norm(cfg_with_map)
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.State"), 4, TS)
    first = n.flush(RECEIVED)
    assert data(first[0]).alarm_code is None
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.AlarmCode"), "ME-CHAIN", TS)
    late = n.flush(RECEIVED)
    assert late[0].ts == TS
    assert data(late[0]).alarm_code == "ME-CHAIN"
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.AlarmCode"), "EL-DRIVE", TS + timedelta(minutes=5))
    cause = n.flush(RECEIVED)
    assert cause[0].ts == TS + timedelta(minutes=5)
    assert data(cause[0]).state == EquipmentState.DOWN_UNPLANNED
    assert data(cause[0]).alarm_code == "EL-DRIVE"
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.State"), 1, TS + timedelta(minutes=9))
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.AlarmCode"), "", TS + timedelta(minutes=9))
    back = n.flush(RECEIVED)
    assert len(back) == 1
    assert data(back[0]).alarm_code is None


def test_redelivery_is_dropped_and_unknown_values_are_dq07(cfg_with_map: TwinConfig) -> None:
    n, b = norm(cfg_with_map)
    level = node(b, "BUFFERS.PBS.Level")
    n.feed(level, 12, TS)
    n.feed(level, 13, TS)  # same instant twice in a batch: the last value wins
    assert [data(e).level for e in n.flush(None)] == [13]
    n.feed(level, 13, TS)  # reconnect re-delivery
    n.feed(level, 11, TS - timedelta(seconds=1))  # older value
    assert n.flush(None) == []
    assert n.dropped == 2
    n.feed(node(b, "WELD.WELD-1.ABB-01.State"), 99, TS)
    assert n.flush(None) == []
    dq = n.drain_dq()
    assert [d.rule_id for d in dq] == ["DQ-07"]
    assert dq[0].details["kind"] == "state_value"
    n.reset()
    n.feed(level, 12, TS)
    assert len(n.flush(None)) == 1


def test_values_kits_telemetry_quality(cfg_with_map: TwinConfig) -> None:
    n, b = norm(cfg_with_map)
    kits = node(b, "CKD.J7.Kits")
    n.feed(kits, 40, TS)
    n.feed(kits, 39, TS + timedelta(minutes=1))
    n.feed(kits, 120, TS + timedelta(minutes=2))
    n.feed(node(b, "ASSY.ASSY-1.CONV-03.vibration_mm_s"), 3.25, TS, "uncertain")
    n.feed(node(b, "BUFFERS.EOL.Capacity"), 12, TS)
    n.feed(node(b, "BUFFERS.EOL.Level"), 4, TS)
    events = n.flush(None)
    assert [data(e).event for e in events if e.kind == "ckd"] == ["set", "consume", "delivery"]
    tele = next(e for e in events if e.kind == "telemetry")
    assert data(tele).unit == "мм/с"
    assert tele.quality == "uncertain"
    eol = next(e for e in events if e.kind == "buffer_level")
    assert data(eol).capacity == 12
    value, ts, quality = mqtt_payload(
        '{"ts":"2026-10-16T02:30:00.000000Z","value":4,"quality":"good"}'
    )
    assert (value, ts, quality) == (4, TS, "good")


def test_opcua_and_mqtt_paths_give_identical_ids(cfg_with_map: TwinConfig) -> None:
    a, b = norm(cfg_with_map, "opcua")
    m, _ = norm(cfg_with_map, "mqtt")
    for nid, topic in (
        ("ASSY.ASSY-1.CONV-03.State", "qost/v1/KST/ASSY/ASSY-1/CONV-03/state"),
        ("ASSY.ASSY-1.CONV-03.AlarmCode", "qost/v1/KST/ASSY/ASSY-1/CONV-03/alarm_code"),
        ("BUFFERS.PBS.Level", "qost/v1/KST/BUFFERS/PBS/level"),
    ):
        value: Any = {"State": 4, "AlarmCode": "ME-CHAIN", "Level": 3}[nid.rsplit(".", 1)[-1]]
        a.feed(node(b, nid), value, TS)
        m.feed(b.topics[topic], value, TS)
    ea, em = a.flush(None), m.flush(None)
    assert [e.event_id for e in ea] == [e.event_id for e in em]
    assert {e.source for e in em} == {"mqtt"}


def test_opcua_fidelity_against_simulator_records(cfg_with_map: TwinConfig) -> None:
    """Records of a simulated hour -> what the OPC UA server writes -> normalizer == sim events."""
    cfg = cfg_with_map
    contract = load_contract(cfg)
    codes = state_codes(contract)
    space = build_address_space(cfg)

    class Capture(OpcUaServer):
        def __init__(self) -> None:
            super().__init__(space, endpoint="opc.tcp://x/", namespace_uri="u", state_codes=codes)
            self.captured: list[tuple[str, Any, datetime]] = []

        async def write(
            self, kind: str, target: str, signal: str, value: Any, ts: datetime
        ) -> None:
            var = self._vars.get((kind, target, signal))
            if var is None:
                return
            if var.datatype == "Int32" and isinstance(value, EquipmentState):
                value = self.state_codes[value]
            self.captured.append((f"ns=2;s={var.ident}", value, ts))

    n, bindings = norm(cfg)
    start = cfg.simulation.clock.demo_start - timedelta(hours=2)
    model = PlantModel(cfg, start=start, telemetry_period_s=300)
    factory = EventFactory.deterministic(site="KST", t0=model.t0, seed=model.seed, nonce="f")
    capture = Capture()
    expected: list[AnyEvent] = []
    got: list[AnyEvent] = []

    async def drive() -> None:
        for step in range(1, 7 * 12 + 1):  # 7 hours in 5-minute ticks
            model.run_until_time(start + timedelta(minutes=5 * step))
            records: list[Rec] = model.drain()
            expected.extend(factory.convert_all(records))
            capture.captured.clear()
            await capture.write_records(model, records)
            for node_id, value, ts in capture.captured:
                binding = bindings.nodes.get(node_id)
                if binding is not None:
                    n.feed(binding, value, ts)
            got.extend(n.flush(None))

    asyncio.run(drive())

    def key(e: AnyEvent) -> tuple[Any, ...]:
        d = data(e)
        if e.kind == "state":
            return (
                e.kind,
                e.entity,
                e.ts,
                str(d.state),
                d.alarm_code if e.entity_type == "equipment" else None,
            )
        if e.kind == "buffer_level":
            return (e.kind, e.entity, e.ts, d.level)
        if e.kind == "telemetry":
            return (e.kind, e.entity, e.ts, d.signal, round(d.value, 4))
        if e.kind == "alarm":
            return (e.kind, e.entity, e.ts, d.active)
        return (e.kind,)

    kinds = {"state", "buffer_level", "telemetry", "alarm"}
    # the sim may emit two buffer levels at one instant; OPC UA keeps the last one
    last: dict[tuple[str, str, datetime, str], AnyEvent] = {}
    for e in expected:
        if e.kind in kinds:
            signal = data(e).signal if e.kind == "telemetry" else ""
            last[(e.kind, e.entity, e.ts, signal)] = e
    want = {key(e) for e in last.values()}
    have = {key(e) for e in got if e.kind in kinds}
    assert len(want) > 500
    assert want == have


# --------------------------------------------------------------------------- spool, outputs


def _events(k: int, base: int = 0) -> list[AnyEvent]:
    ids = RandomIds()
    return [
        make_event(
            "buffer_level",
            event_id=ids(TS + timedelta(seconds=base + i)),
            ts=TS + timedelta(seconds=base + i),
            source="opcua",
            site="KST",
            entity_type="buffer",
            entity="PBS",
            data={"buffer": "PBS", "level": i % 30, "capacity": 30},
        )
        for i in range(k)
    ]


def test_spool_order_segments_cursor_and_torn_tail(tmp_path: Path) -> None:
    spool = Spool(tmp_path, segment_bytes=2000, max_bytes=10**9, fsync=False)
    batches = [Batch.of(_events(3, 10 * i)) for i in range(10)]
    for batch in batches:
        spool.append(batch.line())
    assert len(list(tmp_path.glob("*.jsonl"))) > 1
    seen = []
    for _ in range(4):
        head = spool.peek()
        assert head is not None
        cursor, line = head
        seen.append(Batch.from_line(line).events[0].event_id)
        spool.ack(cursor)
    reopened = Spool(tmp_path, segment_bytes=2000, max_bytes=10**9, fsync=False)
    while (head := reopened.peek()) is not None:
        seen.append(Batch.from_line(head[1]).events[0].event_id)
        reopened.ack(head[0])
    assert seen == [b.events[0].event_id for b in batches]
    assert reopened.empty()
    # a torn tail (crash while appending) is cut on open
    reopened.append(batches[0].line())
    last = sorted(tmp_path.glob("*.jsonl"))[-1]
    with last.open("ab") as fh:
        fh.write(b'[{"event_id": "01')
    repaired = Spool(tmp_path, segment_bytes=2000, max_bytes=10**9, fsync=False)
    head = repaired.peek()
    assert head is not None
    repaired.ack(head[0])
    assert repaired.peek() is None


def test_spool_cap_drops_the_oldest_segment(tmp_path: Path) -> None:
    spool = Spool(tmp_path, segment_bytes=1000, max_bytes=3000, fsync=False)
    for i in range(20):
        spool.append(Batch.of(_events(3, 10 * i)).line())
    assert spool.dropped_segments > 0
    assert spool.size_bytes <= 4000


class FlakyTarget:
    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.received: list[str] = []

    async def write(self, batch: Batch) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("down")
        self.received.extend(e.event_id for e in batch.events)

    async def recover(self) -> None:
        return None


async def test_spooled_output_keeps_order_through_an_outage(tmp_path: Path) -> None:
    target = FlakyTarget(failures=3)
    out = SpooledOutput(target, Spool(tmp_path, segment_bytes=10**6, max_bytes=10**9, fsync=False))
    stop = asyncio.Event()
    task = asyncio.create_task(out.run(stop))
    batches = [Batch.of(_events(5, 10 * i)) for i in range(12)]
    for batch in batches:
        out.submit(batch)
        await asyncio.sleep(0.01)
    for _ in range(300):
        if len(target.received) == 60:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await task
    assert target.received == [e.event_id for b in batches for e in b.events]
    assert out.spooled_batches > 0
    assert out.spool.empty()


async def test_batches_close_by_size_and_time(cfg_with_map: TwinConfig, tmp_path: Path) -> None:
    from redis.asyncio import Redis

    from qost_collector.service import CollectorService
    from qost_collector.settings import CollectorSettings
    from twin_core.clock import ManualClock

    settings = CollectorSettings(
        collector_batch_max=4, collector_batch_ms=50, collector_spool_dir=tmp_path
    )
    service = CollectorService(
        cfg_with_map, settings, ManualClock(TS), Redis.from_url("redis://localhost:1/0")
    )

    class Sink:
        def __init__(self) -> None:
            self.batches: list[Sequence[AnyEvent]] = []

        def submit(self, batch: Batch) -> None:
            self.batches.append(batch.events)

    sink = Sink()
    service.outputs = [sink, sink]  # type: ignore[list-item]
    service.emit(_events(10))
    assert [len(b) for b in sink.batches] == [4, 4, 4, 4]  # two outputs x two full batches
    task = asyncio.create_task(service.batcher())
    await asyncio.sleep(0.15)
    service.stop.set()
    await task
    assert [len(b) for b in sink.batches][-2:] == [2, 2]
    await service.sink.aclose()
