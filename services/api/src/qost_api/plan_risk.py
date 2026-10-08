"""AL-P1 "risk of missing the monthly plan" from the forecast (SPEC §9.7, M6 decision).

After every *baseline* forecast of the current month (``POST /forecast`` without overrides — the
director panel computes it on open) the API evaluates :meth:`AlertEvaluator.plan_risk` with the
effective thresholds (``rules.yaml`` + ``settings``) and upserts the day's AL-P1 alert (entity
``PLANT``, dedup ``AL-P1|PLANT|<date>``): opened/updated when P(target) is below
``plan_risk_warn_p`` / ``plan_risk_crit_p``, resolved when a later baseline is above. What-if
scenarios never raise it. The change is published like any alert change.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI
from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qost_api.events_out import alert_message, open_alert_counts, publish_alert
from qost_api.queries.settings import stored_overrides
from twin_core.alert_text import alert_message_ru
from twin_core.config import TwinConfig
from twin_core.db import AlertRow
from twin_core.forecast.calibration import month_of
from twin_core.rules import AL_PLAN_RISK, PLANT_ENTITY, AlertEvaluator
from twin_core.thresholds import effective_config

log = structlog.get_logger("qost_api.plan_risk")


async def record_plan_risk(
    app: FastAPI, month: str, overrides: dict[str, Any], result: dict[str, Any] | None
) -> str | None:
    """Evaluate AL-P1 on a forecast result; returns ``opened`` / ``resolved`` / ``None``."""
    sessions: async_sessionmaker[AsyncSession] | None = app.state.sessionmaker
    cfg: TwinConfig = app.state.config
    if sessions is None or result is None or overrides:
        return None
    now = app.state.clock.now()
    if month != month_of(cfg, now):
        return None
    day = now.astimezone(cfg.timezone).date()
    key = f"{AL_PLAN_RISK}|{PLANT_ENTITY}|{day.isoformat()}"
    async with sessions() as session:
        stored, _ = await stored_overrides(session)
        eff = effective_config(cfg, stored)
        finding = AlertEvaluator(eff.rules).plan_risk(period_date=day, forecast=result)
        outcome: str | None
        if finding is not None:
            rule = cfg.alert_rules[AL_PLAN_RISK]
            stmt = insert(AlertRow).values(
                ts=now,
                rule_id=AL_PLAN_RISK,
                severity=finding.severity,
                entity_type="site",
                entity=PLANT_ENTITY,
                title_ru=rule.name_ru,
                title_kk=rule.name_kk,
                message_ru=alert_message_ru(finding, eff),
                message_kk=None,
                value=finding.value,
                status="open",
                escalation_level=0,
                dedup_key=key,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["dedup_key"],
                    set_={
                        "ts": stmt.excluded.ts,
                        "severity": stmt.excluded.severity,
                        "value": stmt.excluded.value,
                        "message_ru": stmt.excluded.message_ru,
                        "status": case((AlertRow.status == "ack", "ack"), else_="open"),
                        "resolved_ts": None,
                    },
                )
            )
            outcome = "opened"
        else:
            row: AlertRow | None = await session.scalar(
                select(AlertRow).where(AlertRow.dedup_key == key, AlertRow.status != "resolved")
            )
            if row is None:
                return None
            row.status = "resolved"
            row.resolved_ts = now
            outcome = "resolved"
        await session.commit()
        row = await session.scalar(select(AlertRow).where(AlertRow.dedup_key == key))
        redis = getattr(app.state, "redis", None)
        if row is not None and redis is not None:
            try:
                await publish_alert(
                    redis,
                    app.state.settings,
                    ts=now,
                    data=alert_message(row, cfg),
                    counts=await open_alert_counts(session),
                )
            except (OSError, ConnectionError) as exc:
                log.warning("plan_risk_publish_failed", error=str(exc)[:200])
    return outcome
