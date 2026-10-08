"""Database access of the notifier (SPEC §8): alerts (read), ``telegram_subscription``,
``alert_notification`` and ``audit_log`` (subscription changes, NFR-04)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import asyncpg

from qost_notifier.core import Subscription
from qost_notifier.format import AlertInfo
from twin_core.db.sink import asyncpg_dsn

CHANNEL = "telegram"


@dataclass(frozen=True, slots=True)
class NotificationRow:
    alert_id: int
    recipient: str
    status: str
    """``sent`` | ``failed`` | ``digest`` (queued for the shift digest)."""
    sent_ts: datetime | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PendingDigest:
    id: int
    chat_id: int
    alert: AlertInfo


class Store(Protocol):
    async def find_alert(self, dedup_key: str) -> AlertInfo | None: ...

    async def get_alert(self, alert_id: int) -> AlertInfo | None: ...

    async def subscriptions(self) -> list[Subscription]: ...

    async def subscription(self, chat_id: int) -> Subscription | None: ...

    async def subscribe(self, chat_id: int, role: str, ts: datetime) -> None: ...

    async def unsubscribe(self, chat_id: int, ts: datetime) -> bool: ...

    async def record(self, rows: Sequence[NotificationRow]) -> None: ...

    async def pending_digests(self) -> list[PendingDigest]: ...

    async def mark(
        self, ids: Sequence[int], status: str, ts: datetime, error: str | None = None
    ) -> None: ...

    async def aclose(self) -> None: ...


_ALERT_COLUMNS = (
    "id, dedup_key, rule_id, severity, entity_type, entity, ts, message_ru, value, status, "
    "escalation_level"
)


def _value(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _alert(row: Any, prefix: str = "") -> AlertInfo:
    return AlertInfo(
        dedup_key=row[f"{prefix}dedup_key"],
        rule_id=row[f"{prefix}rule_id"],
        severity=row[f"{prefix}severity"],
        entity_type=row[f"{prefix}entity_type"],
        entity=row[f"{prefix}entity"],
        ts=row[f"{prefix}ts"],
        message_ru=row[f"{prefix}message_ru"],
        value=_value(row[f"{prefix}value"]),
        status=row[f"{prefix}status"],
        escalation_level=int(row[f"{prefix}escalation_level"] or 0),
        id=int(row[f"{prefix}id"]),
    )


class PgStore:
    def __init__(self, database_url: str) -> None:
        self.dsn = asyncpg_dsn(database_url)
        self._pool: asyncpg.Pool | None = None

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4, timeout=10)
        return self._pool

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def find_alert(self, dedup_key: str) -> AlertInfo | None:
        pool = await self.pool()
        row = await pool.fetchrow(
            f"SELECT {_ALERT_COLUMNS} FROM alert WHERE dedup_key = $1", dedup_key
        )
        return _alert(row) if row is not None else None

    async def get_alert(self, alert_id: int) -> AlertInfo | None:
        pool = await self.pool()
        row = await pool.fetchrow(f"SELECT {_ALERT_COLUMNS} FROM alert WHERE id = $1", alert_id)
        return _alert(row) if row is not None else None

    async def subscriptions(self) -> list[Subscription]:
        pool = await self.pool()
        rows = await pool.fetch(
            "SELECT chat_id, role FROM telegram_subscription WHERE active ORDER BY chat_id"
        )
        return [Subscription(int(r["chat_id"]), r["role"]) for r in rows]

    async def subscription(self, chat_id: int) -> Subscription | None:
        pool = await self.pool()
        row = await pool.fetchrow(
            "SELECT chat_id, role FROM telegram_subscription WHERE chat_id = $1 AND active",
            chat_id,
        )
        return Subscription(int(row["chat_id"]), row["role"]) if row is not None else None

    async def _audit(
        self, conn: Any, ts: datetime, action: str, chat_id: int, after: dict[str, Any]
    ) -> None:
        await conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, entity_type, entity_id, before, after) "
            "VALUES ($1, NULL, $2, 'telegram_subscription', $3, NULL, $4::jsonb)",
            ts,
            action,
            str(chat_id),
            json.dumps({**after, "by": f"telegram:{chat_id}", "source": "notifier"}),
        )

    async def subscribe(self, chat_id: int, role: str, ts: datetime) -> None:
        pool = await self.pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO telegram_subscription (chat_id, role, created_ts, active) "
                "VALUES ($1, $2, $3, true) ON CONFLICT (chat_id) DO UPDATE "
                "SET role = EXCLUDED.role, created_ts = EXCLUDED.created_ts, active = true",
                chat_id,
                role,
                ts,
            )
            await self._audit(conn, ts, "telegram.subscribe", chat_id, {"role": role})

    async def unsubscribe(self, chat_id: int, ts: datetime) -> bool:
        pool = await self.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE telegram_subscription SET active = false "
                "WHERE chat_id = $1 AND active RETURNING role",
                chat_id,
            )
            if row is None:
                return False
            await self._audit(conn, ts, "telegram.unsubscribe", chat_id, {"role": row["role"]})
            return True

    async def record(self, rows: Sequence[NotificationRow]) -> None:
        if not rows:
            return
        pool = await self.pool()
        await pool.executemany(
            "INSERT INTO alert_notification (alert_id, channel, recipient, status, sent_ts, error) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            [(r.alert_id, CHANNEL, r.recipient, r.status, r.sent_ts, r.error) for r in rows],
        )

    async def pending_digests(self) -> list[PendingDigest]:
        pool = await self.pool()
        columns = ", ".join(f"a.{c.strip()} AS a_{c.strip()}" for c in _ALERT_COLUMNS.split(","))
        rows = await pool.fetch(
            f"SELECT n.id AS nid, n.recipient, {columns} FROM alert_notification n "
            "JOIN alert a ON a.id = n.alert_id "
            "WHERE n.channel = $1 AND n.status = 'digest' ORDER BY n.id",
            CHANNEL,
        )
        return [PendingDigest(int(r["nid"]), int(r["recipient"]), _alert(r, "a_")) for r in rows]

    async def mark(
        self, ids: Sequence[int], status: str, ts: datetime, error: str | None = None
    ) -> None:
        if not ids:
            return
        pool = await self.pool()
        await pool.execute(
            "UPDATE alert_notification SET status = $2, sent_ts = $3, error = $4 "
            "WHERE id = ANY($1::bigint[])",
            list(ids),
            status,
            ts,
            error,
        )


__all__ = ["CHANNEL", "NotificationRow", "PendingDigest", "PgStore", "Store"]
