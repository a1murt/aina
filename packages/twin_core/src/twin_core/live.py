"""Live contract between the engine (writer) and the api (reader): Redis keys, the ``live``
pub/sub envelope and the §12.3 snapshot (SPEC §9.1, §12.3).

* ``live:{name}`` hashes (field = entity code, value = JSON): ``equipment``, ``lines``,
  ``areas``, ``buffers``, ``downtime_open``; strings (JSON): ``plant``, ``bottleneck``,
  ``alerts_open``, ``meta``. They always hold the current view; the engine rebuilds them after a
  restart or a reset.
* Channel ``live``: ``{"type": …, "ts": "…Z", "data": {…}}`` — exactly the WebSocket envelope of
  §12.3 (``state | unit | buffer | kpi | alert | clock | bottleneck | snapshot``). ``snapshot``
  from the engine means "the view was rebuilt, re-read it" (data: ``{"reason": …}``).
* Stream ``alerts``: new / changed / escalated alerts with recipients and channels (input of the
  notifier, M8). Stream ``events``: the collector's event stream (``k`` kind, ``e`` entity,
  ``j`` event JSON).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from twin_core.clock import ClockState, format_utc, to_plant_tz
from twin_core.config import TwinConfig

LiveType = Literal[
    "snapshot",
    "state",
    "unit",
    "buffer",
    "kpi",
    "alert",
    "clock",
    "bottleneck",
    "forecast_progress",
]
HASHES = ("equipment", "lines", "areas", "buffers", "downtime_open")
STRINGS = ("plant", "bottleneck", "alerts_open", "meta")
EVENTS_STREAM = "events"
ALERTS_STREAM = "alerts"
LIVE_CHANNEL = "live"
LIVE_PREFIX = "live:"
COLLECTOR_STATUS = "collector:status"


@dataclass(frozen=True, slots=True)
class LiveKeys:
    """Key names under a prefix (tests use their own prefixes and channels)."""

    prefix: str = LIVE_PREFIX
    channel: str = LIVE_CHANNEL

    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    @property
    def all(self) -> list[str]:
        return [self.key(n) for n in (*HASHES, *STRINGS)]


def envelope(kind: str, ts: datetime, data: Mapping[str, Any]) -> str:
    """The ``live`` channel message (= WS envelope)."""
    return json.dumps(
        {"type": kind, "ts": format_utc(ts), "data": data}, ensure_ascii=False, default=str
    )


class AsyncRedisLike(Protocol):
    """``redis.asyncio.Redis`` (methods return awaitables)."""

    def hgetall(self, name: str) -> Any: ...

    def get(self, name: str) -> Any: ...


def _decode(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


def _ordered(rows: Mapping[str, Any], order: Sequence[str]) -> list[dict[str, Any]]:
    rank = {code: i for i, code in enumerate(order)}
    items = [dict(v) for v in rows.values() if isinstance(v, dict)]
    return sorted(
        items, key=lambda r: (rank.get(str(r.get("code")), len(rank)), str(r.get("code")))
    )


async def read_views(redis: AsyncRedisLike, keys: LiveKeys) -> dict[str, Any]:
    """Raw content of every ``live:*`` key (hashes as ``{field: value}``)."""
    out: dict[str, Any] = {}
    for name in HASHES:
        raw = await redis.hgetall(keys.key(name))
        out[name] = {
            (k.decode() if isinstance(k, bytes) else str(k)): _decode(v) for k, v in raw.items()
        }
    for name in STRINGS:
        out[name] = _decode(await redis.get(keys.key(name)))
    return out


def build_snapshot(
    views: Mapping[str, Any],
    cfg: TwinConfig,
    clock: ClockState | None,
    *,
    mode: str = "sim",
) -> dict[str, Any]:
    """The ``GET /live/snapshot`` body of SPEC §12.3 from :func:`read_views` and the plant clock."""
    clock_json: dict[str, Any] | None = None
    if clock is not None:
        local = to_plant_tz(clock.plant_time, cfg.timezone)
        shift = cfg.calendar.shift_at(clock.plant_time, working_only=True)
        clock_json = {
            "plant_time": local.isoformat(),
            "shift": None
            if shift is None
            else {
                "date": shift.shift_date.isoformat(),
                "code": shift.code,
                "elapsed_min": round((clock.plant_time - shift.start).total_seconds() / 60.0, 1),
            },
            "speed": 0.0 if clock.paused else clock.speed,
            "mode": mode,
        }
    line_order = list(cfg.flow_lines)
    eq_order = list(cfg.equipment)
    buffer_order = [b.code for b in cfg.plant.buffers]
    lines = []
    for row in _ordered(views.get("lines") or {}, line_order):
        lines.append(
            {
                "code": row.get("code"),
                "state": row.get("state"),
                "since": row.get("since"),
                "reason_code": row.get("reason_code"),
                "pq": row.get("pq"),
                "gq": row.get("gq"),
                "plan_to_now": row.get("plan_to_now"),
                "availability": row.get("availability"),
                "effectiveness": row.get("effectiveness"),
                "quality_ratio": row.get("quality_ratio"),
                "oee": row.get("oee"),
            }
        )
    equipment = [
        {
            "code": row.get("code"),
            "state": row.get("state"),
            "since": row.get("since"),
            "reason_code": row.get("reason_code"),
            "health_index": row.get("health_index"),
            "alarm": bool(row.get("alarm")),
        }
        for row in _ordered(views.get("equipment") or {}, eq_order)
    ]
    buffers = [
        {
            "code": row.get("code"),
            "level": row.get("level"),
            "capacity": row.get("capacity"),
            "minutes_to_full": row.get("minutes_to_full"),
            "minutes_to_empty": row.get("minutes_to_empty"),
        }
        for row in _ordered(views.get("buffers") or {}, buffer_order)
    ]
    bottleneck = views.get("bottleneck") or {}
    return {
        "clock": clock_json,
        "lines": lines,
        "equipment": equipment,
        "buffers": buffers,
        "bottleneck": {
            "current": bottleneck.get("current"),
            "since": bottleneck.get("since"),
            "shift_shares": bottleneck.get("shift_shares") or {},
        },
        "alerts_open": views.get("alerts_open") or {"critical": 0, "warning": 0, "info": 0},
    }


async def read_snapshot(
    redis: AsyncRedisLike,
    cfg: TwinConfig,
    *,
    keys: LiveKeys | None = None,
    clock: ClockState | None = None,
    mode: str = "sim",
) -> dict[str, Any]:
    """Read ``live:*`` and assemble the §12.3 snapshot (api M4 and the integration tests)."""
    views = await read_views(redis, keys or LiveKeys())
    return build_snapshot(views, cfg, clock, mode=mode)


__all__ = [
    "ALERTS_STREAM",
    "COLLECTOR_STATUS",
    "EVENTS_STREAM",
    "HASHES",
    "LIVE_CHANNEL",
    "LIVE_PREFIX",
    "STRINGS",
    "LiveKeys",
    "LiveType",
    "build_snapshot",
    "envelope",
    "read_snapshot",
    "read_views",
]
