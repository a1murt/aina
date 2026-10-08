"""Shift report and notifier storage against TimescaleDB with engine data (SPEC §8, §11.5, §14).

Database ``<TEST_DATABASE_URL db>_it_m8`` (dropped, re-created and migrated for the module):
two weeks of the virtual plant (01–14.10.2026) are written through the collector's sink and
recomputed by the engine replay (M3 path); then ``POST /api/v1/reports/shift`` builds a real
template report from those rows (with the month forecast of the M6 service), and the notifier's
``PgStore`` round-trips subscriptions, notifications and digests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from m3_stack import create_database, drop_database, query, url_string
from sqlalchemy.engine import URL

from api_support import bearer
from qost_api.app import create_app
from qost_engine.replay import run_replay
from qost_notifier.store import NotificationRow, PgStore
from qost_sim.backfill import run_backfill
from support import REPO_ROOT
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.db.sink import DbSink
from twin_core.event_sink import MemorySink
from twin_core.report import SECTIONS, has_sections, word_count

pytestmark = pytest.mark.integration

START = datetime.fromisoformat("2026-10-01T00:00:00+05:00")
END = datetime.fromisoformat("2026-10-15T00:00:00+05:00")
MASTER = bearer("master", username="it-master")


@pytest.fixture(scope="module")
def db_url() -> Iterator[URL]:
    url = create_database("it_m8")
    yield url
    drop_database(url)


@pytest.fixture(scope="module")
def history(db_url: URL, cfg: TwinConfig) -> URL:
    async def build() -> None:
        memory = MemorySink()
        await run_backfill(cfg, memory, start=START, end=END, telemetry_period_s=600)
        sink = DbSink(url_string(db_url))
        try:
            for i in range(0, len(memory.events), 5000):
                await sink.write_batch(memory.events[i : i + 5000])
        finally:
            await sink.aclose()
        await run_replay(cfg, database_url=url_string(db_url), start=START, end=END)

    asyncio.run(build())
    return db_url


def test_template_report_from_engine_data(
    history: URL, cfg: TwinConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "none")
    clock = ManualClock(END)
    app = create_app(cfg, clock=clock, database_url=url_string(history))
    with TestClient(app) as client:
        body = {"date": "2026-10-14", "shift": "A", "lang": "ru"}
        created = client.post("/api/v1/reports/shift", json=body, headers=MASTER)
        assert created.status_code == 201, created.text
        report = created.json()
        kk = client.post("/api/v1/reports/shift", json={**body, "lang": "kk"}, headers=MASTER)
        assert kk.status_code == 201
        assert kk.json()["translation"] == "draft"
        listed = client.get(
            "/api/v1/reports/shift", params={"date": "2026-10-14", "shift": "A"}, headers=MASTER
        ).json()
    assert [r["id"] for r in listed] == [kk.json()["id"], report["id"]]
    assert report["generated_by"] == "template"
    assert report["numbers_verified"] is True
    text = report["text"]
    assert has_sections(text, "ru")
    assert word_count(text) <= 250
    for head in SECTIONS["ru"]:
        assert head in text
    data = report["input"]["shift"]
    assert data["source"] == "events"
    assert data["closed"] is True
    assert [ln["code"] for ln in data["lines"]] == list(cfg.flow_lines)
    assert data["totals"]["output"] > 0
    assert data["bottleneck"], "the engine wrote bottleneck shares for the shift"
    assert data["forecast"] is not None, data["notes"]
    assert data["forecast"]["month"] == "2026-10"
    out = REPO_ROOT / "var" / "m8_report_example.txt"
    out.parent.mkdir(exist_ok=True)
    Path(out).write_text(text + "\n" + kk.json()["text"], encoding="utf-8")
    print("\n" + text)  # shown with -s

    rows = asyncio.run(
        query(
            history,
            "SELECT action, entity_type, after FROM audit_log WHERE action = :a",
            a="report.create",
        )
    )
    assert len(rows) == 2
    after = rows[0]["after"]
    after = json.loads(after) if isinstance(after, str) else after
    assert after["by"] == "it-master"
    assert after["generated_by"] == "template"


async def test_notifier_store_round_trip(history: URL, cfg: TwinConfig) -> None:
    alerts = await query(history, "SELECT id, dedup_key FROM alert ORDER BY id LIMIT 1")
    if not alerts:
        await query(
            history,
            "INSERT INTO alert (ts, rule_id, severity, entity_type, entity, title_ru, message_ru, "
            "value, status, escalation_level, dedup_key) VALUES (:ts, 'AL-S1', 'info', "
            "'equipment', 'WATER-01', 't', 'm', '{}'::jsonb, 'open', 0, 'it|m8')",
            ts=START,
        )
        alerts = await query(history, "SELECT id, dedup_key FROM alert ORDER BY id LIMIT 1")
    alert_id, key = int(alerts[0]["id"]), str(alerts[0]["dedup_key"])
    store = PgStore(url_string(history))
    ts = END
    try:
        found = await store.find_alert(key)
        assert found is not None
        assert found.id == alert_id
        assert (await store.get_alert(alert_id)) == found
        await store.subscribe(7001, "master", ts)
        await store.subscribe(7001, "director", ts + timedelta(minutes=1))  # role change
        assert [(s.chat_id, s.role) for s in await store.subscriptions()] == [(7001, "director")]
        await store.record(
            [
                NotificationRow(alert_id, "7001", "digest", None),
                NotificationRow(alert_id, "7002", "sent", ts),
            ]
        )
        pending = await store.pending_digests()
        assert [(p.chat_id, p.alert.id) for p in pending] == [(7001, alert_id)]
        await store.mark([p.id for p in pending], "sent", ts)
        assert await store.pending_digests() == []
        assert await store.unsubscribe(7001, ts)
        assert not await store.unsubscribe(7001, ts)
        assert await store.subscription(7001) is None
    finally:
        await store.aclose()
    audit = await query(
        history,
        "SELECT action FROM audit_log WHERE entity_type = 'telegram_subscription' ORDER BY id",
    )
    assert [a["action"] for a in audit] == [
        "telegram.subscribe",
        "telegram.subscribe",
        "telegram.unsubscribe",
    ]
    statuses = await query(
        history, "SELECT status FROM alert_notification WHERE alert_id = :i ORDER BY id", i=alert_id
    )
    assert [s["status"] for s in statuses] == ["sent", "sent"]
