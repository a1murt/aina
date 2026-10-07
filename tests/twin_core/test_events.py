"""Unified event schema (SPEC §7.2), ULIDs and event sinks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from twin_core.domain import EquipmentState
from twin_core.event_sink import (
    JsonlSink,
    MemorySink,
    NullSink,
    open_sink,
    register_sink,
    sink_schemes,
)
from twin_core.events import (
    EVENT_CLASSES,
    FIRST_EXIT_RESULTS,
    DeterministicIds,
    RandomIds,
    StateEvent,
    UnitEvent,
    dumps,
    encode_ulid,
    make_event,
    parse_event,
    to_dict,
    ulid_ms,
    ulid_timestamp,
)

TS = datetime(2026, 10, 16, 4, 31, 12, 123000, tzinfo=UTC)
ID = "01JA7Q3M4X2B7Z9K0F6T8R5N1C"

PAYLOADS: dict[str, tuple[str, str, dict[str, object]]] = {
    "state": ("equipment", "CONV-03", {"state": "DOWN_UNPLANNED", "reason_code": "ME-CHAIN"}),
    "unit": (
        "line",
        "ASSY-1",
        {"line": "ASSY-1", "body_id": "B2610160001", "product": "ONIX", "result": "pass"},
    ),
    "defect": (
        "line",
        "PAINT-1",
        {"line": "PAINT-1", "defect_code": "P-DUST", "qty": 1, "disposition": "rework"},
    ),
    "telemetry": (
        "equipment",
        "BOOTH-02",
        {"signal": "filter_dp_pa", "value": 371.5, "unit": "Па"},
    ),
    "alarm": ("equipment", "CONV-03", {"code": "ME-CHAIN", "active": True, "text": "Обрыв цепи"}),
    "buffer_level": ("buffer", "PBS", {"buffer": "PBS", "level": 22, "capacity": 30}),
    "operator": ("line", "ASSY-1", {"action": "andon", "user": "op1", "payload": {"note": "x"}}),
    "ckd": ("product", "J7", {"product": "J7", "kits": 40, "event": "set"}),
}


def build(kind_: str, **overrides: object) -> dict[str, object]:
    kind = kind_
    entity_type, entity, data = PAYLOADS[kind]
    doc: dict[str, object] = {
        "event_id": ID,
        "ts": "2026-10-16T04:31:12.123Z",
        "received_ts": "2026-10-16T04:31:12.480Z",
        "source": "opcua",
        "site": "KST",
        "entity_type": entity_type,
        "entity": entity,
        "kind": kind,
        "data": data,
        "quality": "good",
    }
    doc.update(overrides)
    return doc


@pytest.mark.parametrize("kind", sorted(PAYLOADS))
def test_every_kind_round_trips(kind: str) -> None:
    event = parse_event(build(kind))
    assert isinstance(event, EVENT_CLASSES[kind])
    text = dumps(event)
    again = parse_event(text)
    assert again == event
    assert json.loads(text)["ts"] == "2026-10-16T04:31:12.123000Z"
    assert to_dict(event)["kind"] == kind


def test_spec_example_parses() -> None:
    example = build("state")
    example["data"] = {
        "state": "DOWN_UNPLANNED",
        "reason_code": "ME-CHAIN",
        "alarm_code": "E-CHAIN",
    }
    event = parse_event(json.dumps(example))
    assert isinstance(event, StateEvent)
    assert event.data.state is EquipmentState.DOWN_UNPLANNED
    assert event.ts == TS


def test_timestamps_are_normalized_to_utc() -> None:
    plus5 = timezone(timedelta(hours=5))
    event = make_event(
        "ckd",
        event_id=ID,
        ts=datetime(2026, 10, 16, 9, 31, 12, tzinfo=plus5),
        source="sim",
        site="KST",
        entity_type="product",
        entity="J7",
        data={"product": "J7", "kits": 3, "event": "consume"},
    )
    assert event.ts.utcoffset() == timedelta(0)
    assert dumps(event).count('"ts":"2026-10-16T04:31:12.000000Z"') == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"ts": "2026-10-16T04:31:12"},  # naive
        {"kind": "status"},  # unknown kind
        {"event_id": "not-a-ulid"},
        {"source": "plc"},
        {"entity": "conv-03"},  # codes are UPPER-KEBAB
        {"quality": "excellent"},
        {"data": {"state": "BROKEN"}},
        {"kind": "unit"},  # payload of another kind
        {"extra": 1},
    ],
)
def test_invalid_events_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_event(build("state", **overrides))


def test_unit_results_contract() -> None:
    assert {"pass", "defect", "scrap"} == FIRST_EXIT_RESULTS
    doc = build("unit")
    doc["data"] = {**PAYLOADS["unit"][2], "result": "rework_pass", "defect_code": "A-TORQUE"}
    event = parse_event(doc)
    assert isinstance(event, UnitEvent)
    assert event.data.result not in FIRST_EXIT_RESULTS


def test_ulid_encoding_and_time() -> None:
    ulid = encode_ulid(ulid_ms(TS), bytes(range(10)))
    assert len(ulid) == 26
    assert ulid_timestamp(ulid) == TS
    assert encode_ulid(0, bytes(10)) == "0" * 26
    assert encode_ulid((1 << 48) - 1, b"\xff" * 10) == "7" + "Z" * 25
    with pytest.raises(ValueError, match="10 bytes"):
        encode_ulid(1, b"short")
    with pytest.raises(ValueError, match="out of range"):
        encode_ulid(-1, bytes(10))
    with pytest.raises(ValueError, match="invalid ULID"):
        ulid_timestamp("U" * 26)


def test_ulids_sort_by_time() -> None:
    ids = RandomIds()
    earlier, later = ids(TS), ids(TS + timedelta(milliseconds=5))
    assert earlier < later
    assert ids(TS) != ids(TS)


def test_deterministic_ids_repeat_for_same_seed_and_nonce() -> None:
    first, second = DeterministicIds(7, "backfill"), DeterministicIds(7, "backfill")
    other = DeterministicIds(7, "live:1")
    a = [first(TS) for _ in range(5)]
    assert a == [second(TS) for _ in range(5)]
    assert len(set(a)) == 5
    assert set(a).isdisjoint(other(TS) for _ in range(5))
    assert first.issued == 5


# --------------------------------------------------------------------------- sinks


def events(n: int) -> list[object]:
    return [parse_event(build("ckd", event_id=encode_ulid(i, bytes(10)))) for i in range(n)]


async def test_memory_and_null_sinks() -> None:
    memory, null = MemorySink(), NullSink()
    batch = events(3)
    await memory.write(batch)  # type: ignore[arg-type]
    await null.write(batch)  # type: ignore[arg-type]
    await memory.aclose()
    await null.aclose()
    assert len(memory.events) == 3
    assert memory.batches == 1
    assert memory.closed
    assert null.count == 3


async def test_jsonl_sink_writes_one_event_per_line(tmp_path: Path) -> None:
    sink = open_sink(f"jsonl:{tmp_path / 'out' / 'events.jsonl'}")
    assert isinstance(sink, JsonlSink)
    await sink.write(events(2))  # type: ignore[arg-type]
    await sink.write(events(1))  # type: ignore[arg-type]
    await sink.aclose()
    await sink.aclose()
    lines = (tmp_path / "out" / "events.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 3
    assert all(parse_event(line).kind == "ckd" for line in lines)
    with pytest.raises(RuntimeError, match="closed"):
        await sink.write(events(1))  # type: ignore[arg-type]


def test_sink_registry() -> None:
    assert {"jsonl", "memory", "null"} <= set(sink_schemes())
    assert isinstance(open_sink("memory:"), MemorySink)
    assert isinstance(open_sink("null:"), NullSink)
    register_sink("test-memory", lambda _arg: MemorySink())
    assert isinstance(open_sink("test-memory:x"), MemorySink)
    with pytest.raises(ValueError, match="unknown event sink"):
        open_sink("db:")
    with pytest.raises(ValueError, match="needs a path"):
        open_sink("jsonl:")
    with pytest.raises(ValueError, match="unknown event sink"):
        open_sink("no-colon")
