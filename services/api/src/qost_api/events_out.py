"""What the API writes towards the engine and the live view.

* Operator events (§7.2 ``operator``: ``andon | classify_downtime | log_defect | material_call``)
  go into ``event_raw`` (the journal the engine replays and recovers gaps from) in the request's
  transaction and into the Redis stream ``events`` (fields ``k``/``e``/``j`` like the
  collector) before the commit; a failed ``XADD`` rolls the request back (503), so an accepted
  action always reaches the engine. The engine is the single writer of ``downtime`` (M3), so a
  classification is an event, not an ``UPDATE``.
* Alert changes made by the API (ack/resolve, operator calls, AL-P1) are published like the
  engine does: an ``alert`` envelope on the ``live`` channel, an entry in the ``alerts`` stream
  (notifier input) and a refreshed ``live:alerts_open`` count.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.auth import Principal
from qost_api.settings import ApiSettings
from twin_core.config import TwinConfig
from twin_core.db import AlertRow, EventRaw
from twin_core.events import OperatorAction, OperatorData, OperatorEvent, RandomIds
from twin_core.live import LiveKeys, envelope

_ids = RandomIds()


def operator_event(
    cfg: TwinConfig,
    principal: Principal,
    *,
    ts: datetime,
    action: OperatorAction,
    entity_type: str,
    entity: str,
    payload: dict[str, Any],
) -> OperatorEvent:
    return OperatorEvent.model_validate(
        {
            "event_id": _ids(ts),
            "ts": ts,
            "received_ts": ts,
            "source": "operator",
            "site": cfg.plant.site.code,
            "entity_type": entity_type,
            "entity": entity,
            "data": OperatorData(action=action, user=principal.username, payload=payload),
        }
    )


async def store_event(session: AsyncSession, event: OperatorEvent) -> None:
    """Add the event to ``event_raw`` (idempotent by id) in the session's transaction."""
    await session.execute(
        insert(EventRaw)
        .values(
            event_id=event.event_id,
            ts=event.ts,
            received_ts=event.received_ts,
            source=event.source,
            entity_type=event.entity_type,
            entity=event.entity,
            kind=event.kind,
            data=event.data.model_dump(mode="json"),
            quality=event.quality,
        )
        .on_conflict_do_nothing()
    )


async def stream_event(redis: Redis, settings: ApiSettings, event: OperatorEvent) -> None:
    """``XADD events`` in the collector's format (``k`` kind, ``e`` entity, ``j`` JSON)."""
    await redis.xadd(
        settings.events_stream,
        {"k": event.kind, "e": event.entity, "j": event.model_dump_json()},
        maxlen=settings.events_stream_maxlen,
        approximate=True,
    )


def alert_message(row: AlertRow, cfg: TwinConfig, **extra: Any) -> dict[str, Any]:
    """The ``alert`` envelope data (same keys as the engine's, plus ``id`` and ack fields)."""
    rule = cfg.alert_rules.get(row.rule_id)
    return {
        "id": row.id,
        "dedup_key": row.dedup_key,
        "ts": row.ts.isoformat(),
        "rule_id": row.rule_id,
        "severity": row.severity,
        "entity_type": row.entity_type,
        "entity": row.entity,
        "title_ru": row.title_ru,
        "message_ru": row.message_ru,
        "value": row.value,
        "status": row.status,
        "ack_ts": row.ack_ts.isoformat() if row.ack_ts else None,
        "resolved_ts": row.resolved_ts.isoformat() if row.resolved_ts else None,
        "escalation_level": row.escalation_level,
        "recipients": list(rule.recipients) if rule else [],
        "channels": list(rule.channels) if rule else [],
        **extra,
    }


async def open_alert_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(AlertRow.severity, func.count())
        .where(AlertRow.status == "open")
        .group_by(AlertRow.severity)
    )
    counts = {"critical": 0, "warning": 0, "info": 0}
    for severity, n in rows.all():
        counts[str(severity)] = int(n)
    return counts


async def publish_alert(
    redis: Redis,
    settings: ApiSettings,
    *,
    ts: datetime,
    data: dict[str, Any],
    counts: dict[str, int] | None,
) -> None:
    """Publish an alert change: live channel, ``alerts`` stream, ``live:alerts_open``."""
    keys = LiveKeys(settings.live_prefix, settings.live_channel)
    pipe = redis.pipeline(transaction=False)
    if counts is not None:
        pipe.set(keys.key("alerts_open"), json.dumps(counts, ensure_ascii=False))
    pipe.publish(keys.channel, envelope("alert", ts, data))
    pipe.xadd(
        settings.alerts_stream,
        {"j": json.dumps(data, ensure_ascii=False, default=str)},
        maxlen=settings.alerts_stream_maxlen,
        approximate=True,
    )
    await pipe.execute()
