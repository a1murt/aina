"""The ``GET /live/snapshot`` body (§12.3) — shared by the REST route and the WebSocket."""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from qost_api.deps import current_clock_state, live_keys
from qost_api.settings import ApiSettings
from twin_core.clock import Clock
from twin_core.config import TwinConfig
from twin_core.live import build_snapshot, read_views


async def snapshot(
    redis: Redis, cfg: TwinConfig, clock: Clock, settings: ApiSettings
) -> dict[str, Any]:
    """The §12.3 snapshot plus ``areas``, ``plant`` and ``downtime_open`` (same keys, superset)."""
    views = await read_views(redis, live_keys(settings))
    body = build_snapshot(
        views,
        cfg,
        current_clock_state(clock),
        mode="sim" if settings.clock_mode == "sim" else "system",
    )
    order = {code: i for i, code in enumerate(cfg.areas)}
    body["areas"] = sorted(
        (dict(v) for v in (views.get("areas") or {}).values() if isinstance(v, dict)),
        key=lambda r: order.get(str(r.get("code")), len(order)),
    )
    body["plant"] = views.get("plant")
    eq_order = {code: i for i, code in enumerate([*cfg.equipment, *cfg.lines])}
    body["downtime_open"] = sorted(
        (dict(v) for v in (views.get("downtime_open") or {}).values() if isinstance(v, dict)),
        key=lambda r: (eq_order.get(str(r.get("entity")), len(eq_order)), str(r.get("start_ts"))),
    )
    meta = views.get("meta") or {}
    body["engine"] = {"ts": meta.get("engine_ts"), "epoch": meta.get("epoch")}
    return body
