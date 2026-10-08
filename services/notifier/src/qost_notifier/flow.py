"""Subscription by role PIN (SPEC §14): ``/start`` -> role -> PIN -> ``telegram_subscription``.

A pure state machine per chat; the bot handlers feed it and act on its replies. Wrong PINs are
limited to :data:`MAX_ATTEMPTS` per role choice; PINs compare in constant time.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from qost_notifier import texts
from qost_notifier.messenger import Button

MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    keyboard: tuple[tuple[Button, ...], ...] | None = None
    subscribe_role: str | None = None
    """Set when the PIN was right: the caller writes the subscription."""


@dataclass(slots=True)
class _Pending:
    role: str
    attempts: int = 0


class SubscriptionFlow:
    def __init__(self, pins: dict[str, str], *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.pins = dict(pins)
        self.max_attempts = max_attempts
        self._pending: dict[int, _Pending] = {}

    def waiting_pin(self, chat_id: int) -> bool:
        return chat_id in self._pending

    def start(self, chat_id: int) -> Reply:
        self._pending.pop(chat_id, None)
        if not self.pins:
            return Reply(texts.NO_ROLES)
        rows = tuple(
            (Button(texts.ROLE_NAMES.get(role, role), callback=f"role:{role}"),)
            for role in self.pins
        )
        return Reply(texts.START, rows)

    def choose(self, chat_id: int, role: str) -> Reply:
        if role not in self.pins:
            self._pending.pop(chat_id, None)
            return Reply(texts.UNKNOWN_ROLE)
        self._pending[chat_id] = _Pending(role)
        return Reply(texts.ASK_PIN.format(role=texts.ROLE_NAMES.get(role, role)))

    def pin(self, chat_id: int, text: str) -> Reply:
        pending = self._pending.get(chat_id)
        if pending is None:
            return Reply(texts.NOT_SUBSCRIBED)
        expected = self.pins[pending.role]
        if hmac.compare_digest(text.strip().encode(), expected.encode()):
            del self._pending[chat_id]
            name = texts.ROLE_NAMES.get(pending.role, pending.role)
            return Reply(texts.SUBSCRIBED.format(role=name), subscribe_role=pending.role)
        pending.attempts += 1
        left = self.max_attempts - pending.attempts
        if left <= 0:
            del self._pending[chat_id]
            return Reply(texts.TOO_MANY)
        return Reply(texts.WRONG_PIN.format(left=left))


__all__ = ["MAX_ATTEMPTS", "Reply", "SubscriptionFlow"]
