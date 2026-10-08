"""Who gets which alert, when (SPEC §9.7, §14) — pure decisions, no I/O.

* Only rules with the ``telegram`` channel reach Telegram; resolved and acknowledged alerts are
  not sent.
* Recipients: the rule's ``recipients`` plus the escalation chain up to the alert's level; an
  escalation event (``notify``) goes to the roles it names.
* Dedup: one message per (``dedup_key``, chat) within the window (wall time) unless the severity
  rose or the escalation level grew.
* ``info``: never sent one by one — queued for the shift digest (once per alert and chat).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from qost_notifier.format import AlertInfo, parse_ts
from twin_core.config import TwinConfig

TELEGRAM = "telegram"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """An entry of the engine's ``alerts`` stream (M3 contract)."""

    dedup_key: str
    escalation_level: int | None = None
    """Set for an escalation event; ``notify`` then lists the roles of that level."""
    notify: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_escalation(self) -> bool:
        return self.escalation_level is not None

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> StreamEvent:
        if "escalation_level" in payload and "rule_id" not in payload:
            return cls(
                dedup_key=str(payload["dedup_key"]),
                escalation_level=int(payload["escalation_level"]),
                notify=tuple(str(r) for r in payload.get("notify") or ()),
                data=payload,
            )
        return cls(dedup_key=str(payload["dedup_key"]), data=payload)


@dataclass(frozen=True, slots=True)
class Subscription:
    chat_id: int
    role: str


@dataclass(frozen=True, slots=True)
class Delivery:
    chat_id: int
    role: str
    mode: Literal["send", "digest"]


class Deduper:
    """(dedup_key, chat) -> last (time, severity rank, escalation level) within the window."""

    def __init__(self, window_s: float, now: Callable[[], float] = time.monotonic) -> None:
        self.window_s = window_s
        self._now = now
        self._seen: dict[tuple[str, int, str], tuple[float, int, int]] = {}

    def allow(self, key: str, chat_id: int, severity: str, level: int, *, mode: str) -> bool:
        now = self._now()
        if len(self._seen) > 10_000:
            self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self.window_s}
        rank = SEVERITY_RANK.get(severity, 0)
        slot = (key, chat_id, mode)
        prev = self._seen.get(slot)
        fresh = prev is not None and now - prev[0] < self.window_s
        if mode == "digest" and prev is not None:
            return False  # queued once per alert and chat
        if fresh and prev is not None and rank <= prev[1] and level <= prev[2]:
            return False
        self._seen[slot] = (now, rank, level)
        return True


def recipient_roles(cfg: TwinConfig, alert: AlertInfo, event: StreamEvent) -> set[str]:
    if event.is_escalation:
        return set(event.notify)
    rule = cfg.alert_rules.get(alert.rule_id)
    roles = set(rule.recipients) if rule is not None else set()
    esc = cfg.rules.escalation.get(alert.severity)  # type: ignore[call-overload]
    if esc is not None and alert.escalation_level > 0:
        roles.update(esc.chain[: alert.escalation_level + 1])
    return roles


def allowed_ack_roles(cfg: TwinConfig, rule_id: str, severity: str) -> set[str]:
    """Roles that may accept an alert from Telegram: recipients, escalation chain, admin."""
    rule = cfg.alert_rules.get(rule_id)
    roles = set(rule.recipients) if rule is not None else set()
    esc = cfg.rules.escalation.get(severity)  # type: ignore[call-overload]
    if esc is not None:
        roles.update(esc.chain)
    roles.add("admin")
    return roles


def plan_deliveries(
    cfg: TwinConfig,
    alert: AlertInfo,
    event: StreamEvent,
    subscriptions: Iterable[Subscription],
    dedup: Deduper,
) -> list[Delivery]:
    rule = cfg.alert_rules.get(alert.rule_id)
    if rule is None or TELEGRAM not in rule.channels:
        return []
    if alert.status != "open":
        return []
    roles = recipient_roles(cfg, alert, event)
    mode: Literal["send", "digest"] = "digest" if alert.severity == "info" else "send"
    level = event.escalation_level or alert.escalation_level
    out: list[Delivery] = []
    seen: set[int] = set()
    for sub in subscriptions:
        if sub.role not in roles or sub.chat_id in seen:
            continue
        seen.add(sub.chat_id)
        if dedup.allow(alert.dedup_key, sub.chat_id, alert.severity, level, mode=mode):
            out.append(Delivery(sub.chat_id, sub.role, mode))
    return out


def merge_alert(event: StreamEvent, row: AlertInfo | None) -> AlertInfo | None:
    """Stream data is fresher than the row for upserts (the engine publishes before it
    commits); the row gives the id, the ack status and, for escalations, everything."""
    data = event.data
    if event.is_escalation or "rule_id" not in data:
        if row is None:
            return None
        level = event.escalation_level if event.escalation_level is not None else 0
        return AlertInfo(
            dedup_key=row.dedup_key,
            rule_id=row.rule_id,
            severity=row.severity,
            entity_type=row.entity_type,
            entity=row.entity,
            ts=row.ts,
            message_ru=row.message_ru,
            value=row.value,
            status=row.status,
            escalation_level=max(level, row.escalation_level),
            id=row.id,
        )
    status = str(data.get("status") or "open")
    if row is not None and row.status in ("ack", "resolved") and status == "open":
        status = row.status
    return AlertInfo(
        dedup_key=event.dedup_key,
        rule_id=str(data["rule_id"]),
        severity=str(data.get("severity") or "info"),
        entity_type=str(data.get("entity_type") or ""),
        entity=str(data.get("entity") or ""),
        ts=parse_ts(data.get("ts")) or (row.ts if row is not None else _EPOCH),
        message_ru=str(data.get("message_ru") or ""),
        value=data.get("value"),
        status=status,
        escalation_level=row.escalation_level if row is not None else 0,
        id=row.id if row is not None else None,
    )


__all__ = [
    "SEVERITY_RANK",
    "TELEGRAM",
    "Deduper",
    "Delivery",
    "StreamEvent",
    "Subscription",
    "allowed_ack_roles",
    "merge_alert",
    "plan_deliveries",
    "recipient_roles",
]
