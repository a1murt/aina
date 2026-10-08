"""Sending to Telegram with retries (SPEC §14): a small :class:`Messenger` interface over the
aiogram ``Bot`` (tests use fakes), exponential backoff for transient errors.

Transient (retried): network errors, Telegram 5xx, ``RetryAfter`` (waits what Telegram asks).
Permanent (not retried): bad request, bot blocked by the user (the subscription is switched off
by the caller), chat not found.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = structlog.get_logger("qost_notifier.messenger")


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback: str | None = None
    url: str | None = None


Keyboard = Sequence[Sequence[Button]]


class TransientSendError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentSendError(Exception):
    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked
        """The user blocked the bot or the chat is gone: deactivate the subscription."""


class Messenger(Protocol):
    async def send(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int: ...

    async def edit_keyboard(
        self, chat_id: int, message_id: int, keyboard: Keyboard | None
    ) -> None: ...

    async def edit_text(
        self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None
    ) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...


def markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.callback, url=b.url) for b in row]
            for row in keyboard
        ]
    )


class AiogramMessenger:
    """:class:`Messenger` over ``aiogram.Bot``; aiogram errors -> transient/permanent."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def _call[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            return await fn()
        except TelegramRetryAfter as exc:
            raise TransientSendError(str(exc), retry_after=float(exc.retry_after)) from exc
        except (TelegramNetworkError, TelegramServerError) as exc:
            raise TransientSendError(str(exc)) from exc
        except (TelegramForbiddenError, TelegramNotFound) as exc:
            raise PermanentSendError(str(exc), blocked=True) from exc
        except TelegramBadRequest as exc:
            raise PermanentSendError(str(exc)) from exc

    async def send(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int:
        message = await self._call(
            lambda: self.bot.send_message(chat_id, text, reply_markup=markup(keyboard))
        )
        return int(message.message_id)

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        await self._call(
            lambda: self.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=markup(keyboard)
            )
        )

    async def edit_text(
        self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None
    ) -> None:
        await self._call(
            lambda: self.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup(keyboard)
            )
        )

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self._call(lambda: self.bot.delete_message(chat_id, message_id))


Sleep = Callable[[float], Awaitable[None]]


async def with_retries[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_s: float = 1.0,
    max_s: float = 30.0,
    sleep: Sleep = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Run ``fn``; on :class:`TransientSendError` wait ``base × 2^k`` (± jitter, capped, or the
    server's ``retry_after``) and try again, at most ``attempts`` times in total."""
    for attempt in range(attempts):
        try:
            return await fn()
        except TransientSendError as exc:
            if attempt == attempts - 1:
                raise
            delay = min(base_s * (2**attempt), max_s) * (0.5 + jitter() / 2)
            if exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            log.info("telegram_retry", attempt=attempt + 1, delay_s=round(delay, 2), error=str(exc))
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = [
    "AiogramMessenger",
    "Button",
    "Keyboard",
    "Messenger",
    "PermanentSendError",
    "TransientSendError",
    "markup",
    "with_retries",
]
