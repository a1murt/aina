"""Entry point: ``python -m qost_notifier`` (SPEC §14).

With ``OFFLINE=true`` or an empty ``TELEGRAM_BOT_TOKEN`` the bot is not started: the process
serves ``/healthz`` + ``/readyz`` (healthy no-op) and logs ``notifier_disabled`` with the reason —
no connection to Telegram is ever opened (NFR-04/05). Otherwise: Telegram long polling, the
``alerts`` stream consumer and the digest timer, until SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from qost_notifier.settings import NotifierSettings
from twin_core.health import load_config_or_exit, start_health_server
from twin_core.log import configure_logging

SERVICE = "notifier"
DEFAULT_PORT = 8130
"""``HEALTH_PORT`` default (compose healthcheck)."""


def disabled_reason(settings: NotifierSettings) -> str | None:
    if settings.offline:
        return "OFFLINE=true: Telegram is an external service"
    if not settings.token:
        return "TELEGRAM_BOT_TOKEN is empty"
    return None


async def _wait_for_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run(settings: NotifierSettings | None = None) -> None:
    s = settings or NotifierSettings()
    log = configure_logging(SERVICE)
    cfg = load_config_or_exit(SERVICE)
    reason = disabled_reason(s)
    if reason is not None:
        server = await start_health_server(
            SERVICE,
            port=s.health_port,
            stats=lambda: {"enabled": False, "reason": reason},
        )
        log.warning("notifier_disabled", reason=reason, port=s.health_port)
        try:
            await _wait_for_signal()
        finally:
            server.close()
            await server.wait_closed()
        return

    from aiogram import Bot, Dispatcher
    from redis.asyncio import Redis

    from qost_notifier.api_client import HttpAckClient
    from qost_notifier.bot import build_router
    from qost_notifier.messenger import AiogramMessenger
    from qost_notifier.service import Notifier
    from qost_notifier.store import PgStore
    from twin_core.clock import RedisKV, SimClock, create_clock

    redis = Redis.from_url(s.redis_url)
    clock = create_clock(s, RedisKV(redis))
    bot = Bot(s.token)
    store = PgStore(s.database_url)
    ack = HttpAckClient(s.notifier_api_url, s.notifier_api_user, s.api_password)
    notifier = Notifier(cfg, s, clock, store, AiogramMessenger(bot), ack)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(notifier))
    stop = asyncio.Event()
    server = await start_health_server(
        SERVICE, port=s.health_port, stats=lambda: {"enabled": True, **notifier.stats}
    )
    log.info("notifier_started", roles=sorted(s.pins), port=s.health_port)
    async with contextlib.AsyncExitStack() as stack:
        if isinstance(clock, SimClock):
            await stack.enter_async_context(clock)
        tasks = [
            asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False)),
            asyncio.create_task(notifier.consume(redis, stop)),
            asyncio.create_task(notifier.digest_loop(stop)),
        ]
        try:
            await _wait_for_signal()
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await dispatcher.stop_polling()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            server.close()
            await server.wait_closed()
            await ack.aclose()
            await store.aclose()
            await bot.session.close()
            await redis.aclose()
            log.info("notifier_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
