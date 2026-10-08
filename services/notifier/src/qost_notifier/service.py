"""Notifier service (SPEC §14): ``alerts`` stream -> Telegram, subscriptions, ack, digests.

* Consumes the engine's ``alerts`` Redis stream with the consumer group ``notifier`` (pending
  entries first after a restart, then new ones), XACK after each entry is handled.
* For each entry: the ``alert`` row (id, ack status) — waiting up to ``NOTIFIER_ALERT_WAIT_S``
  for the engine's commit —, recipients and dedup (:mod:`qost_notifier.core`), send with
  retries, one ``alert_notification`` row per chat (``sent`` | ``failed`` | ``digest``).
* ``info`` alerts are queued (``digest``) and sent as one message per chat when their shift is
  over in plant time (``Clock``).
* Buttons: «Принять» -> ``POST /api/v1/alerts/{id}/ack`` as the service account (the chat's
  role must be a recipient of the rule); «Открыть» -> ``PUBLIC_WEB_URL`` deep link.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog

from qost_notifier import texts
from qost_notifier.api_client import AckClient
from qost_notifier.core import (
    TELEGRAM,
    Deduper,
    StreamEvent,
    allowed_ack_roles,
    merge_alert,
    plan_deliveries,
)
from qost_notifier.flow import Reply, SubscriptionFlow
from qost_notifier.format import (
    AlertInfo,
    ack_mark,
    alert_message,
    button_url_allowed,
    digest_message,
    open_url,
)
from qost_notifier.messenger import (
    Button,
    Keyboard,
    Messenger,
    PermanentSendError,
    TransientSendError,
    with_retries,
)
from qost_notifier.settings import NotifierSettings
from qost_notifier.store import NotificationRow, PendingDigest, Store
from twin_core.clock import Clock, ClockNotReadyError, to_plant_tz
from twin_core.config import TwinConfig

log = structlog.get_logger("qost_notifier")

Sleep = Callable[[float], Awaitable[None]]


class Notifier:
    def __init__(
        self,
        cfg: TwinConfig,
        settings: NotifierSettings,
        clock: Clock,
        store: Store,
        messenger: Messenger,
        ack_client: AckClient,
        *,
        dedup: Deduper | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.settings = settings
        self.clock = clock
        self.store = store
        self.messenger = messenger
        self.ack_client = ack_client
        self.dedup = dedup or Deduper(settings.notifier_dedup_window_s, monotonic)
        self.flow = SubscriptionFlow(settings.pins)
        self.sleep = sleep
        self.monotonic = monotonic
        self.stats: Counter[str] = Counter()

    # ------------------------------------------------------------------ helpers

    def now(self) -> datetime | None:
        try:
            return self.clock.now()
        except ClockNotReadyError:
            return None

    async def _send(self, chat_id: int, text: str, keyboard: Keyboard | None) -> int:
        s = self.settings
        return await with_retries(
            lambda: self.messenger.send(chat_id, text, keyboard),
            attempts=s.notifier_send_attempts,
            base_s=s.notifier_backoff_s,
            max_s=s.notifier_backoff_max_s,
            sleep=self.sleep,
        )

    async def _row(self, event: StreamEvent) -> AlertInfo | None:
        deadline = self.monotonic() + self.settings.notifier_alert_wait_s
        while True:
            row = await self.store.find_alert(event.dedup_key)
            if row is not None or self.monotonic() >= deadline:
                return row
            await self.sleep(0.25)

    def compose(self, alert: AlertInfo, event: StreamEvent) -> tuple[str, Keyboard]:
        url = open_url(self.settings.public_web_url, self.settings.notifier_open_path, alert.id)
        buttons = [Button(texts.BUTTON_ACK, callback=f"ack:{alert.id}")]
        in_text: str | None = None
        if url is not None:
            if button_url_allowed(url):
                buttons.append(Button(texts.BUTTON_OPEN, url=url))
            else:
                in_text = url
        text = alert_message(
            self.cfg,
            alert,
            escalation_level=event.escalation_level if event.is_escalation else None,
            link_in_text=in_text,
        )
        return text, (tuple(buttons),)

    # ------------------------------------------------------------------ alerts stream

    async def handle_event(self, payload: dict[str, Any]) -> list[NotificationRow]:
        event = StreamEvent.parse(payload)
        self.stats["events"] += 1
        status = str(payload.get("status") or "")
        if not event.is_escalation and status and status != "open":
            self.stats["skipped_status"] += 1
            return []
        channels = payload.get("channels")
        if not event.is_escalation and isinstance(channels, list) and TELEGRAM not in channels:
            self.stats["skipped_channel"] += 1  # ui-only rule: no need to wait for its row
            return []
        alert = merge_alert(event, await self._row(event))
        if alert is None:
            self.stats["skipped_no_alert"] += 1
            return []
        deliveries = plan_deliveries(
            self.cfg, alert, event, await self.store.subscriptions(), self.dedup
        )
        if not deliveries:
            return []
        if alert.id is None:
            self.stats["skipped_not_committed"] += 1
            log.warning("alert_row_missing", dedup_key=alert.dedup_key)
            return []
        alert_id = alert.id
        text, keyboard = self.compose(alert, event)

        async def deliver(chat_id: int, mode: str) -> NotificationRow:
            if mode == "digest":
                self.stats["digest_queued"] += 1
                return NotificationRow(alert_id, str(chat_id), "digest", None)
            try:
                await self._send(chat_id, text, keyboard)
            except TransientSendError as exc:
                self.stats["failed"] += 1
                return NotificationRow(alert_id, str(chat_id), "failed", self.now(), str(exc)[:500])
            except PermanentSendError as exc:
                self.stats["failed"] += 1
                if exc.blocked:
                    ts = self.now()
                    if ts is not None:
                        await self.store.unsubscribe(chat_id, ts)
                return NotificationRow(alert_id, str(chat_id), "failed", self.now(), str(exc)[:500])
            self.stats["sent"] += 1
            return NotificationRow(alert_id, str(chat_id), "sent", self.now())

        rows = list(await asyncio.gather(*(deliver(d.chat_id, d.mode) for d in deliveries)))
        await self.store.record(rows)
        log.info(
            "alert_notified",
            dedup_key=alert.dedup_key,
            escalation=event.escalation_level,
            sent=sum(r.status == "sent" for r in rows),
            failed=sum(r.status == "failed" for r in rows),
            digest=sum(r.status == "digest" for r in rows),
        )
        return rows

    # ------------------------------------------------------------------ digests

    def _shift_label(self, moment: datetime) -> str:
        shift = self.cfg.calendar.shift_at(moment)
        if shift is None:
            return to_plant_tz(moment, self.cfg.timezone).strftime("%d.%m.%Y")
        return f"{shift.code} {shift.shift_date.strftime('%d.%m.%Y')}"

    def _due(self, alert: AlertInfo) -> datetime:
        return self.cfg.calendar.next_shift_change(alert.ts) or alert.ts

    async def flush_digests(self) -> int:
        """Send the digests whose shift is over; returns the number of messages sent."""
        now = self.now()
        if now is None:
            return 0
        groups: dict[tuple[int, str], list[PendingDigest]] = defaultdict(list)
        for item in await self.store.pending_digests():
            if self._due(item.alert) <= now:
                groups[(item.chat_id, self._shift_label(item.alert.ts))].append(item)
        sent = 0
        for (chat_id, label), items in groups.items():
            ids = [i.id for i in items]
            unique = {i.alert.id: i.alert for i in items}  # an alert queued twice is listed once
            text = digest_message(self.cfg, label, list(unique.values()))
            try:
                await self._send(chat_id, text, None)
            except (TransientSendError, PermanentSendError) as exc:
                await self.store.mark(ids, "failed", now, str(exc)[:500])
                self.stats["digest_failed"] += 1
                continue
            await self.store.mark(ids, "sent", now)
            self.stats["digest_sent"] += 1
            sent += 1
        return sent

    # ------------------------------------------------------------------ bot actions

    async def pin(self, chat_id: int, text: str) -> Reply:
        reply = self.flow.pin(chat_id, text)
        if reply.subscribe_role is not None:
            ts = self.now()
            if ts is None:
                return Reply(texts.SUBSCRIBE_FAILED)
            await self.store.subscribe(chat_id, reply.subscribe_role, ts)
            self.stats["subscribed"] += 1
            log.info("telegram_subscribed", chat_id=chat_id, role=reply.subscribe_role)
        return reply

    async def unsubscribe(self, chat_id: int) -> str:
        ts = self.now()
        if ts is not None and await self.store.unsubscribe(chat_id, ts):
            return texts.UNSUBSCRIBED
        return texts.NOT_SUBSCRIBED

    async def ack(
        self, chat_id: int, alert_id: int, message_id: int | None, message_text: str | None
    ) -> str:
        """«Принять»: returns the callback answer; marks the message on success."""
        sub = await self.store.subscription(chat_id)
        if sub is None:
            return texts.NOT_SUBSCRIBED
        alert = await self.store.get_alert(alert_id)
        if alert is None:
            return texts.ACK_FAILED
        if sub.role not in allowed_ack_roles(self.cfg, alert.rule_id, alert.severity):
            return texts.ACK_FORBIDDEN
        result = "already" if alert.status != "open" else await self.ack_client.ack(alert_id)
        self.stats[f"ack_{result}"] += 1
        log.info("telegram_ack", chat_id=chat_id, role=sub.role, alert_id=alert_id, result=result)
        if result not in ("ok", "already"):
            return texts.ACK_FORBIDDEN if result == "forbidden" else texts.ACK_FAILED
        now = self.now()
        if message_id is not None and message_text is not None and now is not None:
            with contextlib.suppress(TransientSendError, PermanentSendError):
                await self.messenger.edit_text(
                    chat_id, message_id, f"{message_text}\n{ack_mark(sub.role, now, self.cfg)}"
                )
        return texts.ACK_DONE if result == "ok" else texts.ACK_ALREADY

    # ------------------------------------------------------------------ loops

    async def consume(self, redis: Any, stop: asyncio.Event) -> None:
        stream, group = self.settings.alerts_stream, self.settings.notifier_group
        try:
            await redis.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as exc:  # BUSYGROUP: the group exists (restart)
            if "BUSYGROUP" not in str(exc):
                raise
        last = "0"
        while not stop.is_set():
            try:
                reply = await redis.xreadgroup(
                    group,
                    self.settings.notifier_consumer,
                    {stream: last},
                    count=50,
                    block=1000,
                )
            except (OSError, ConnectionError) as exc:
                log.warning("stream_read_failed", error=str(exc)[:200])
                await self.sleep(1.0)
                continue
            entries = [e for _, items in reply or [] for e in items]
            if last == "0" and not entries:
                last = ">"
                continue
            for entry_id, fields in entries:
                raw = fields.get(b"j") or fields.get("j")
                try:
                    if raw is not None:
                        await self.handle_event(json.loads(raw))
                except Exception as exc:
                    self.stats["errors"] += 1
                    log.exception("alert_event_failed", entry=str(entry_id), error=str(exc)[:200])
                await redis.xack(stream, group, entry_id)

    async def digest_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.flush_digests()
            except Exception as exc:
                log.warning("digest_failed", error=str(exc)[:200])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.settings.notifier_digest_check_s)


__all__ = ["Notifier"]
