"""T-LLM (notifier part, SPEC §14): message format, recipients, dedup, info digest, retries,
PIN subscription, ack button — with fakes (no Telegram, no database, no network)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update
from pydantic import SecretStr

from qost_engine.core.effects import AlertUpsert
from qost_notifier import texts
from qost_notifier.__main__ import disabled_reason, run
from qost_notifier.api_client import AckResult, HttpAckClient
from qost_notifier.bot import build_router
from qost_notifier.core import Deduper, StreamEvent, Subscription, plan_deliveries
from qost_notifier.flow import SubscriptionFlow
from qost_notifier.format import (
    AlertInfo,
    alert_message,
    button_url_allowed,
    headline,
    impact_short,
)
from qost_notifier.messenger import (
    Keyboard,
    PermanentSendError,
    TransientSendError,
    with_retries,
)
from qost_notifier.service import Notifier
from qost_notifier.settings import NotifierSettings, parse_role_pins
from qost_notifier.store import NotificationRow, PendingDigest
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig

T0 = datetime(2026, 10, 15, 4, 31, tzinfo=UTC)  # 09:31 plant time, shift A
S1_VALUE = {
    "started": "2026-10-15T04:31:00+00:00",
    "ended": None,
    "elapsed_min": 12.0,
    "reason_code": "ME-CHAIN",
    "line": "ASSY-1",
    "criticality": "A",
    "microstop": False,
    "impact": {
        "lost_min": 12.0,
        "lost_units": 14.2,
        "bottleneck": False,
        "bottleneck_line": "PAINT-1",
        "irrecoverable_units": 0.0,
        "recover_shifts": 0.5,
    },
}


def upsert(
    key: str = "AL-S1|CONV-03|2026-10-15T04:31:00Z",
    *,
    rule: str = "AL-S1",
    severity: str = "critical",
    entity: str = "CONV-03",
    value: Any = None,
    status: str = "open",
    message: str = "Конвейер-03 (финальная): обрыв цепи",
) -> dict[str, Any]:
    """A stream entry exactly as the engine publishes it (M3 contract)."""
    effect = AlertUpsert(
        dedup_key=key,
        ts=T0,
        rule_id=rule,
        severity=severity,
        entity_type="equipment",
        entity=entity,
        title_ru="t",
        message_ru=message,
        value=S1_VALUE if value is None else value,
        status=status,
        resolved_ts=None,
        recipients=("master", "maintenance"),
        channels=("ui", "telegram"),
    )
    payload: dict[str, Any] = json.loads(json.dumps(effect.message(), ensure_ascii=False))
    return payload


def row_of(payload: dict[str, Any], alert_id: int, **changes: Any) -> AlertInfo:
    info = AlertInfo(
        dedup_key=payload["dedup_key"],
        rule_id=payload["rule_id"],
        severity=payload["severity"],
        entity_type=payload["entity_type"],
        entity=payload["entity"],
        ts=T0,
        message_ru=payload["message_ru"],
        value=payload["value"],
        status="open",
        id=alert_id,
    )
    return replace(info, **changes)


# ---------------------------------------------------------------------------- fakes


@dataclass
class FakeStore:
    alerts: dict[str, AlertInfo] = field(default_factory=dict)
    subs: dict[int, str] = field(default_factory=dict)
    rows: list[NotificationRow] = field(default_factory=list)
    marks: list[tuple[list[int], str]] = field(default_factory=list)
    audit: list[tuple[str, int, str]] = field(default_factory=list)
    misses: int = 0
    """``find_alert`` returns None this many times first (engine commit lag)."""

    async def find_alert(self, dedup_key: str) -> AlertInfo | None:
        if self.misses > 0:
            self.misses -= 1
            return None
        return self.alerts.get(dedup_key)

    async def get_alert(self, alert_id: int) -> AlertInfo | None:
        return next((a for a in self.alerts.values() if a.id == alert_id), None)

    async def subscriptions(self) -> list[Subscription]:
        return [Subscription(c, r) for c, r in sorted(self.subs.items())]

    async def subscription(self, chat_id: int) -> Subscription | None:
        role = self.subs.get(chat_id)
        return Subscription(chat_id, role) if role else None

    async def subscribe(self, chat_id: int, role: str, ts: datetime) -> None:
        self.subs[chat_id] = role
        self.audit.append(("telegram.subscribe", chat_id, role))

    async def unsubscribe(self, chat_id: int, ts: datetime) -> bool:
        role = self.subs.pop(chat_id, None)
        if role:
            self.audit.append(("telegram.unsubscribe", chat_id, role))
        return role is not None

    async def record(self, rows: Sequence[NotificationRow]) -> None:
        self.rows.extend(rows)

    async def pending_digests(self) -> list[PendingDigest]:
        done = {i for ids, _ in self.marks for i in ids}
        return [
            PendingDigest(n, int(r.recipient), self._by_id(r.alert_id))
            for n, r in enumerate(self.rows)
            if r.status == "digest" and n not in done
        ]

    def _by_id(self, alert_id: int) -> AlertInfo:
        return next(a for a in self.alerts.values() if a.id == alert_id)

    async def mark(
        self, ids: Sequence[int], status: str, ts: datetime, error: str | None = None
    ) -> None:
        self.marks.append((list(ids), status))

    async def aclose(self) -> None:
        return None


@dataclass
class FakeMessenger:
    sent: list[tuple[int, str, Keyboard | None]] = field(default_factory=list)
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)

    async def send(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((chat_id, text, keyboard))
        return len(self.sent)

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        return None

    async def edit_text(
        self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None
    ) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete(self, chat_id: int, message_id: int) -> None:
        return None


@dataclass
class FakeAck:
    result: AckResult = "ok"
    calls: list[int] = field(default_factory=list)

    async def ack(self, alert_id: int) -> AckResult:
        self.calls.append(alert_id)
        return self.result

    async def aclose(self) -> None:
        return None


class Ticker:
    """Monotonic time under test control."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Rig:
    notifier: Notifier
    store: FakeStore
    messenger: FakeMessenger
    ack: FakeAck
    clock: ManualClock
    ticker: Ticker
    sleeps: list[float]


def make_rig(cfg: TwinConfig, **settings: Any) -> Rig:
    s = NotifierSettings(
        telegram_role_pins="director:1111,master:2222,maintenance:3333,quality:4444",
        public_web_url="https://twin.plant.kz",
        **settings,
    )
    store, messenger, ack = FakeStore(), FakeMessenger(), FakeAck()
    clock = ManualClock(T0 + timedelta(minutes=1))
    ticker = Ticker()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        ticker.t += delay

    notifier = Notifier(cfg, s, clock, store, messenger, ack, sleep=sleep, monotonic=ticker)
    return Rig(notifier, store, messenger, ack, clock, ticker, sleeps)


@pytest.fixture
def rig(cfg: TwinConfig) -> Rig:
    r = make_rig(cfg)
    r.store.subs.update({101: "master", 102: "maintenance", 103: "director", 104: "quality"})
    return r


# ---------------------------------------------------------------------------- format


def test_s1_message_has_the_spec_format(cfg: TwinConfig) -> None:
    alert = row_of(upsert(), 7)
    assert headline(cfg, alert) == (
        "[КРИТИЧНО] Конвейер-03 (финальная) — внеплановая остановка оборудования (обрыв цепи) · "
        "09:31 · Сборка-1 · влияние: −14,2 авто, отыгрывается за ~0,5 смены"
    )
    text = alert_message(cfg, alert, escalation_level=1, link_in_text="http://localhost:3000/x")
    assert text.splitlines() == [
        "Эскалация (уровень 1): не принято за 10 мин",
        headline(cfg, alert),
        "Простой: 12 мин",
        "Открыть: http://localhost:3000/x",
    ]


def test_impact_wording() -> None:
    assert impact_short({"lost_units": 0}) == "влияние на выпуск: нет"
    assert impact_short({"lost_units": 3.0, "bottleneck": True}) == (
        "влияние: −3 авто, безвозвратно (узкое место)"
    )
    assert impact_short({"lost_units": 3.25, "recover_shifts": None}) == (
        "влияние: −3,2 авто, не отыгрывается (нет запаса мощности)"
    )
    assert impact_short({"lost_units": 5.0, "recover_shifts": 1.5, "irrecoverable_units": 1.0}) == (
        "влияние: −5 авто, отыгрывается за ~1,5 смены (~1 безвозвратно)"
    )


def test_other_rules_carry_their_message(cfg: TwinConfig) -> None:
    payload = upsert(
        "AL-Q1|PAINT|2026-10-15/A",
        rule="AL-Q1",
        severity="warning",
        entity="PAINT",
        value=0.0472,
        message="Окраска: брак 4,72% (15.10.2026, смена A), норма 2%",
    )
    text = alert_message(cfg, row_of(payload, 8))
    assert text.splitlines() == [
        "[ВНИМАНИЕ] Окраска — брак выше нормы · 09:31",
        "Окраска: брак 4,72% (15.10.2026, смена A), норма 2%",
    ]


def test_localhost_links_cannot_be_buttons() -> None:
    assert button_url_allowed("https://twin.plant.kz/live?alert=1")
    assert not button_url_allowed("http://localhost:3000/live?alert=1")
    assert not button_url_allowed("http://127.0.0.1:3000/")


def test_service_account_password_defaults_to_the_demo_password() -> None:
    empty = NotifierSettings(notifier_api_password=SecretStr(""), demo_password=SecretStr("d"))
    assert empty.api_password == "d"
    own = NotifierSettings(notifier_api_password=SecretStr("p"), demo_password=SecretStr("d"))
    assert own.api_password == "p"


def test_role_pins_parsing() -> None:
    assert parse_role_pins("director:1111, master:2222,bad,:1,x:") == {
        "director": "1111",
        "master": "2222",
    }


# ---------------------------------------------------------------------------- routing / dedup


def test_recipients_channels_and_status(cfg: TwinConfig) -> None:
    subs = [Subscription(1, "master"), Subscription(2, "director"), Subscription(3, "quality")]
    dedup = Deduper(600, Ticker())
    s1 = upsert()
    plan = plan_deliveries(cfg, row_of(s1, 1), StreamEvent.parse(s1), subs, dedup)
    assert [(d.chat_id, d.mode) for d in plan] == [(1, "send")]  # master; maintenance absent
    o1 = upsert("AL-O1|PAINT-1|2026-10-15/A", rule="AL-O1", severity="warning", entity="PAINT-1")
    assert plan_deliveries(cfg, row_of(o1, 2), StreamEvent.parse(o1), subs, dedup) == []  # ui only
    acked = row_of(s1, 1, status="ack")
    assert plan_deliveries(cfg, acked, StreamEvent.parse(s1), subs, Deduper(600, Ticker())) == []
    escalated = row_of(s1, 1, escalation_level=2)
    roles = plan_deliveries(cfg, escalated, StreamEvent.parse(s1), subs, Deduper(600, Ticker()))
    assert [d.chat_id for d in roles] == [1, 2]  # chain master -> maintenance -> director


def test_dedup_window_severity_and_escalation() -> None:
    ticker = Ticker()
    d = Deduper(600, ticker)
    assert d.allow("k", 1, "warning", 0, mode="send")
    assert not d.allow("k", 1, "warning", 0, mode="send")
    assert d.allow("k", 2, "warning", 0, mode="send")  # another chat
    assert d.allow("k", 1, "critical", 0, mode="send")  # severity rose
    assert not d.allow("k", 1, "warning", 0, mode="send")
    assert d.allow("k", 1, "critical", 1, mode="send")  # escalation level grew
    ticker.t += 601
    assert d.allow("k", 1, "critical", 1, mode="send")  # window passed
    assert d.allow("k", 1, "info", 0, mode="digest")
    ticker.t += 10_000
    assert not d.allow("k", 1, "info", 0, mode="digest")  # queued once per alert and chat


# ---------------------------------------------------------------------------- service


async def test_new_alert_is_sent_with_buttons_and_recorded(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    rows = await rig.notifier.handle_event(payload)
    assert [(r.recipient, r.status) for r in rows] == [("101", "sent"), ("102", "sent")]
    assert all(r.alert_id == 7 and r.sent_ts == rig.clock.now() for r in rows)
    chat, text, keyboard = rig.messenger.sent[0]
    assert chat == 101
    assert text.startswith("[КРИТИЧНО] Конвейер-03 (финальная)")
    assert keyboard is not None
    buttons = list(keyboard[0])
    assert (buttons[0].text, buttons[0].callback) == ("Принять", "ack:7")
    assert (buttons[1].text, buttons[1].url) == ("Открыть", "https://twin.plant.kz/live?alert=7")
    assert rig.store.rows == rows


async def test_value_updates_within_ten_minutes_are_deduplicated(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    await rig.notifier.handle_event(payload)
    later = upsert(value={**S1_VALUE, "elapsed_min": 17.0})
    assert await rig.notifier.handle_event(later) == []
    rig.ticker.t += 601
    assert len(await rig.notifier.handle_event(later)) == 2
    assert len(rig.messenger.sent) == 4


async def test_ui_only_rules_skip_the_database(rig: Rig) -> None:
    payload = upsert("AL-O1|PAINT-1|2026-10-15/A", rule="AL-O1", severity="warning")
    payload["channels"] = ["ui"]
    assert await rig.notifier.handle_event(payload) == []
    assert rig.sleeps == []
    assert rig.notifier.stats["skipped_channel"] == 1


async def test_resolved_and_acknowledged_alerts_are_not_sent(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7, status="ack")
    assert await rig.notifier.handle_event(payload) == []
    assert await rig.notifier.handle_event(upsert(status="resolved")) == []
    assert rig.messenger.sent == []


async def test_waits_for_the_engine_commit(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    rig.store.misses = 3
    rows = await rig.notifier.handle_event(payload)
    assert len(rows) == 2
    assert rig.sleeps == [0.25, 0.25, 0.25]


async def test_uncommitted_alert_is_skipped_after_the_wait(rig: Rig) -> None:
    rows = await rig.notifier.handle_event(upsert())
    assert rows == []
    assert rig.messenger.sent == []
    assert rig.notifier.stats["skipped_not_committed"] == 1
    assert sum(rig.sleeps) >= rig.notifier.settings.notifier_alert_wait_s


async def test_escalation_goes_to_the_next_level(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7, escalation_level=1)
    event = {"dedup_key": payload["dedup_key"], "escalation_level": 1, "notify": ["maintenance"]}
    rows = await rig.notifier.handle_event(event)
    assert [r.recipient for r in rows] == ["102"]
    assert rig.messenger.sent[0][1].startswith("Эскалация (уровень 1): не принято за 10 мин\n")


async def test_info_alerts_go_to_the_shift_digest(rig: Rig, cfg: TwinConfig) -> None:
    payload = upsert(
        "AL-S1|WATER-01|2026-10-15T04:31:00Z",
        severity="info",
        entity="WATER-01",
        value={**S1_VALUE, "line": "QC-1", "impact": None},
    )
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 9)
    rows = await rig.notifier.handle_event(payload)
    assert [(r.recipient, r.status, r.sent_ts) for r in rows] == [
        ("101", "digest", None),
        ("102", "digest", None),
    ]
    assert rig.messenger.sent == []
    assert await rig.notifier.flush_digests() == 0  # shift A still running
    rig.clock.set(datetime(2026, 10, 15, 10, 0, 1, tzinfo=UTC))  # 15:00:01 plant time
    assert await rig.notifier.flush_digests() == 2
    chat, text, keyboard = rig.messenger.sent[0]
    assert chat == 101
    assert text.splitlines()[0] == "Дайджест смены A 15.10.2026: информационные оповещения (1)"
    assert text.splitlines()[1].startswith("- Камера дождевания — внеплановая остановка")
    assert keyboard is None
    assert rig.store.marks == [([0], "sent"), ([1], "sent")]
    assert await rig.notifier.flush_digests() == 0


async def test_transient_errors_are_retried_with_backoff(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    rig.store.subs = {101: "master"}
    rig.messenger.failures = [TransientSendError("502"), TransientSendError("timeout")]
    rows = await rig.notifier.handle_event(payload)
    assert [r.status for r in rows] == ["sent"]
    assert len(rig.sleeps) == 2
    assert 0.5 <= rig.sleeps[0] <= 1.0
    assert 1.0 <= rig.sleeps[1] <= 2.0


async def test_retries_give_up_and_blocked_chats_are_unsubscribed(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    rig.messenger.failures = [TransientSendError("down")] * 5 + [
        PermanentSendError("bot was blocked by the user", blocked=True)
    ]
    rows = await rig.notifier.handle_event(payload)
    assert [(r.recipient, r.status) for r in rows] == [("101", "failed"), ("102", "failed")]
    assert rows[0].error == "down"
    assert "blocked" in (rows[1].error or "")
    assert 102 not in rig.store.subs
    assert 101 in rig.store.subs


async def test_with_retries_backoff_and_retry_after() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientSendError("429", retry_after=7)
        if calls < 4:
            raise TransientSendError("502")
        return "ok"

    result = await with_retries(flaky, attempts=5, base_s=1, max_s=3, sleep=sleep, jitter=lambda: 1)
    assert result == "ok"
    assert sleeps == [7.0, 2.0, 3.0]


# ---------------------------------------------------------------------------- PIN and ack


def test_pin_flow() -> None:
    flow = SubscriptionFlow({"master": "2222", "director": "1111"})
    start = flow.start(5)
    assert start.text == texts.START
    assert start.keyboard is not None
    assert [row[0].callback for row in start.keyboard] == ["role:master", "role:director"]
    assert flow.choose(5, "admin").text == texts.UNKNOWN_ROLE
    assert flow.choose(5, "master").text == "Введите PIN роли «Мастер смены»."
    assert flow.pin(5, "1111").text == texts.WRONG_PIN.format(left=2)
    ok = flow.pin(5, " 2222 ")
    assert ok.subscribe_role == "master"
    assert not flow.waiting_pin(5)
    flow.choose(6, "director")
    for _ in range(2):
        assert flow.pin(6, "0000").subscribe_role is None
    assert flow.pin(6, "0000").text == texts.TOO_MANY
    assert not flow.waiting_pin(6)
    assert SubscriptionFlow({}).start(1).text == texts.NO_ROLES


async def test_ack_checks_the_role_and_marks_the_message(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    assert await rig.notifier.ack(999, 7, 1, "text") == texts.NOT_SUBSCRIBED
    assert await rig.notifier.ack(104, 7, 1, "text") == texts.ACK_FORBIDDEN  # quality
    assert rig.ack.calls == []
    assert await rig.notifier.ack(101, 7, 1, "Конвейер") == texts.ACK_DONE
    assert rig.ack.calls == [7]
    assert rig.messenger.edits == [(101, 1, "Конвейер\nПринято: Мастер смены, 09:32")]
    rig.ack.result = "error"
    assert await rig.notifier.ack(103, 7, 2, "x") == texts.ACK_FAILED  # director: chain role
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7, status="ack")
    assert await rig.notifier.ack(102, 7, 3, "x") == texts.ACK_ALREADY
    assert rig.ack.calls == [7, 7]


# ---------------------------------------------------------------------------- aiogram wiring


class FakeSession(BaseSession):
    """Captures Bot API calls; no network."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - the aiogram session signature
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime(2026, 10, 15, tzinfo=UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(  # pragma: no cover - not used by the bot
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - the aiogram session signature
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        chunks: list[bytes] = []
        for chunk in chunks:
            yield chunk
        raise NotImplementedError

    def names(self) -> list[str]:
        return [type(c).__name__ for c in self.calls]


def update(bot: Bot, n: int, *, text: str | None = None, data: str | None = None) -> Update:
    user = {"id": 501, "is_bot": False, "first_name": "Мастер"}
    message = {
        "message_id": 40 + n,
        "date": 1_760_000_000,
        "chat": {"id": 501, "type": "private"},
        "from": user,
        "text": text or "Конвейер-03 (финальная) — внеплановая остановка",
    }
    if data is None:
        return Update.model_validate({"update_id": n, "message": message}, context={"bot": bot})
    query = {"id": f"q{n}", "from": user, "chat_instance": "c", "data": data, "message": message}
    return Update.model_validate({"update_id": n, "callback_query": query}, context={"bot": bot})


async def test_bot_subscription_and_ack_through_aiogram(cfg: TwinConfig) -> None:
    r = make_rig(cfg)
    payload = upsert()
    r.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    session = FakeSession()
    bot = Bot("42:TEST-TOKEN", session=session)
    dp = Dispatcher()
    dp.include_router(build_router(r.notifier))

    await dp.feed_update(bot, update(bot, 1, text="/start"))
    start = session.calls[-1]
    assert isinstance(start, SendMessage)
    assert start.text == texts.START
    assert start.reply_markup is not None

    await dp.feed_update(bot, update(bot, 2, data="role:master"))
    assert session.names()[-2:] == ["AnswerCallbackQuery", "SendMessage"]
    assert "Мастер смены" in str(getattr(session.calls[-1], "text", ""))

    await dp.feed_update(bot, update(bot, 3, text="2222"))
    assert r.store.subs == {501: "master"}
    assert r.store.audit == [("telegram.subscribe", 501, "master")]
    assert session.names()[-2:] == ["DeleteMessage", "SendMessage"]  # the PIN is removed

    await dp.feed_update(bot, update(bot, 4, data="ack:7"))
    assert r.ack.calls == [7]
    answer = session.calls[-1]
    assert type(answer).__name__ == "AnswerCallbackQuery"
    assert getattr(answer, "text", None) == texts.ACK_DONE

    await dp.feed_update(bot, update(bot, 5, text="/stop"))
    assert r.store.subs == {}
    await bot.session.close()


# ---------------------------------------------------------------------------- disabled mode


def test_offline_or_no_token_disables_the_bot() -> None:
    assert (
        disabled_reason(NotifierSettings(offline=True, telegram_bot_token=SecretStr("1:x")))
        is not None
    )
    assert "TOKEN" in (disabled_reason(NotifierSettings(offline=False)) or "")
    assert (
        disabled_reason(NotifierSettings(offline=False, telegram_bot_token=SecretStr("1:x")))
        is None
    )


async def test_disabled_notifier_is_a_healthy_no_op(
    monkeypatch: pytest.MonkeyPatch, unused_port: int
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Telegram must not be touched with OFFLINE=true")

    monkeypatch.setattr(Bot, "__init__", forbidden)
    settings = NotifierSettings(
        offline=True, telegram_bot_token=SecretStr("1:x"), health_port=unused_port
    )
    task = asyncio.create_task(run(settings))
    try:
        for _ in range(100):
            with contextlib.suppress(OSError):
                reader, writer = await asyncio.open_connection("127.0.0.1", unused_port)
                writer.write(b"GET /stats HTTP/1.1\r\nHost: t\r\n\r\n")
                await writer.drain()
                raw = await reader.read()
                writer.close()
                await writer.wait_closed()
                body = json.loads(raw.partition(b"\r\n\r\n")[2])
                assert body["enabled"] is False
                assert "OFFLINE" in body["reason"]
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("health server did not start")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def unused_port() -> int:
    from sim_support import free_port

    return free_port()


# ---------------------------------------------------------------------------- API client


async def test_ack_client_logs_in_and_retries_on_401(unused_port: int) -> None:
    from aiohttp import web

    logins: list[dict[str, Any]] = []
    acks: list[tuple[str, str]] = []
    tokens = iter(["t1", "t2"])

    async def login(request: web.Request) -> web.Response:
        logins.append(await request.json())
        return web.json_response({"access_token": next(tokens), "token_type": "bearer"})

    async def ack(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        acks.append((request.match_info["id"], auth))
        if auth == "Bearer t1":
            return web.json_response({}, status=401)  # expired token -> one fresh login
        status = {"7": 200, "8": 409, "9": 403, "10": 404}.get(request.match_info["id"], 500)
        return web.json_response({}, status=status)

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_post("/api/v1/alerts/{id}/ack", ack)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_port).start()
    client = HttpAckClient(f"http://127.0.0.1:{unused_port}/", "admin", "qost2026")
    try:
        assert await client.ack(7) == "ok"
        assert [await client.ack(n) for n in (8, 9, 10, 11)] == [
            "already",
            "forbidden",
            "not_found",
            "error",
        ]
    finally:
        await client.aclose()
        await runner.cleanup()
    assert logins == [{"username": "admin", "password": "qost2026"}] * 2
    assert acks[:2] == [("7", "Bearer t1"), ("7", "Bearer t2")]


async def test_ack_client_reports_an_unreachable_api(unused_port: int) -> None:
    client = HttpAckClient(f"http://127.0.0.1:{unused_port}", "admin", "x", timeout_s=1)
    try:
        assert await client.ack(1) == "error"
    finally:
        await client.aclose()


# ---------------------------------------------------------------- stream and aiogram errors


class FakeRedis:
    def __init__(
        self, entries: list[tuple[bytes, dict[bytes, bytes]]], stop: asyncio.Event
    ) -> None:
        self.entries = entries
        self.stop = stop
        self.groups: list[tuple[str, str, str]] = []
        self.reads: list[str] = []
        self.acked: list[bytes] = []

    async def xgroup_create(self, stream: str, group: str, id: str, mkstream: bool) -> None:
        self.groups.append((stream, group, id))
        if len(self.groups) > 1:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")

    async def xreadgroup(
        self, group: str, consumer: str, streams: dict[str, str], count: int, block: int
    ) -> Any:
        last = next(iter(streams.values()))
        self.reads.append(last)
        if last == "0":
            return [[b"alerts", []]]
        if self.entries:
            batch, self.entries = self.entries, []
            return [[b"alerts", batch]]
        self.stop.set()
        return []

    async def xack(self, stream: str, group: str, entry_id: bytes) -> None:
        self.acked.append(entry_id)


async def test_stream_consumer_handles_and_acks(rig: Rig) -> None:
    payload = upsert()
    rig.store.alerts[payload["dedup_key"]] = row_of(payload, 7)
    raw = json.dumps(payload, ensure_ascii=False).encode()
    stop = asyncio.Event()
    redis = FakeRedis([(b"1-0", {b"j": raw}), (b"2-0", {b"j": b"not json"})], stop)
    await rig.notifier.consume(redis, stop)
    assert redis.groups == [("alerts", "notifier", "$")]
    assert redis.reads[:2] == ["0", ">"]
    assert redis.acked == [b"1-0", b"2-0"]  # a broken entry is acknowledged, not retried forever
    assert len(rig.messenger.sent) == 2
    assert rig.notifier.stats["errors"] == 1
    stop.clear()
    await rig.notifier.consume(FakeRedis([], stop), stop)  # existing group: BUSYGROUP is fine


async def test_aiogram_errors_map_to_retry_decisions() -> None:
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNetworkError,
        TelegramRetryAfter,
    )

    from qost_notifier.messenger import AiogramMessenger, Button

    method = SendMessage(chat_id=1, text="x")
    errors: list[Exception] = [
        TelegramRetryAfter(method, "Too Many Requests", retry_after=3),
        TelegramNetworkError(method, "timeout"),
        TelegramForbiddenError(method, "bot was blocked by the user"),
        TelegramBadRequest(method, "message is not modified"),
    ]

    class FailingSession(FakeSession):
        async def make_request(
            self,
            bot: Bot,
            method: TelegramMethod[Any],
            timeout: int | None = None,  # noqa: ASYNC109 - the aiogram session signature
        ) -> Any:
            if errors:
                raise errors.pop(0)
            return await super().make_request(bot, method, timeout)

    session = FailingSession()
    messenger = AiogramMessenger(Bot("42:TEST-TOKEN", session=session))
    keyboard = ((Button("Принять", callback="ack:1"), Button("Открыть", url="https://x.kz/")),)
    with pytest.raises(TransientSendError) as retry:
        await messenger.send(1, "x", keyboard)
    assert retry.value.retry_after == 3.0
    with pytest.raises(TransientSendError):
        await messenger.send(1, "x")
    with pytest.raises(PermanentSendError) as blocked:
        await messenger.edit_text(1, 2, "y")
    assert blocked.value.blocked
    with pytest.raises(PermanentSendError) as bad:
        await messenger.edit_keyboard(1, 2, None)
    assert not bad.value.blocked
    assert await messenger.send(1, "ok", keyboard) > 0
    await messenger.delete(1, 2)
    sent = [c for c in session.calls if isinstance(c, SendMessage)]
    markup_ = sent[-1].reply_markup
    assert markup_ is not None
    assert session.names()[-1] == "DeleteMessage"
    await messenger.bot.session.close()
