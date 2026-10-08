"""aiogram 3 handlers (SPEC §14): ``/start`` -> role buttons -> PIN, ``/stop``, «Принять»."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from qost_notifier import texts
from qost_notifier.messenger import markup
from qost_notifier.service import Notifier


def build_router(notifier: Notifier) -> Router:
    router = Router(name="qost-notifier")

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        reply = notifier.flow.start(message.chat.id)
        await message.answer(reply.text, reply_markup=markup(reply.keyboard))

    @router.message(Command("stop"))
    async def on_stop(message: Message) -> None:
        await message.answer(await notifier.unsubscribe(message.chat.id))

    @router.callback_query(F.data.startswith("role:"))
    async def on_role(query: CallbackQuery) -> None:
        chat = query.message.chat.id if query.message is not None else query.from_user.id
        reply = notifier.flow.choose(chat, (query.data or "").removeprefix("role:"))
        await query.answer()
        if query.bot is not None:
            await query.bot.send_message(chat, reply.text)

    @router.callback_query(F.data.startswith("ack:"))
    async def on_ack(query: CallbackQuery) -> None:
        raw = (query.data or "").removeprefix("ack:")
        if not raw.isdigit():
            await query.answer(texts.ACK_FAILED)
            return
        message = query.message
        chat = message.chat.id if message is not None else query.from_user.id
        message_id = message.message_id if message is not None else None
        text = message.text if isinstance(message, Message) else None
        await query.answer(await notifier.ack(chat, int(raw), message_id, text))

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        chat = message.chat.id
        if not notifier.flow.waiting_pin(chat):
            return
        reply = await notifier.pin(chat, message.text or "")
        with contextlib.suppress(TelegramAPIError):
            await message.delete()  # do not keep PINs in the chat history
        await message.answer(reply.text)

    return router


__all__ = ["build_router"]
