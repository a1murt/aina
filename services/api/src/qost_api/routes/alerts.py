"""``GET /alerts`` (all roles), ``POST /alerts/{id}/ack|resolve`` (rule recipients + admin).

Who may act on an alert: ``admin``, the rule's ``recipients`` and the roles of its escalation
chain up to the current ``escalation_level`` (an alert escalated to the director can be
acknowledged by the director). The route admits every role that can be a recipient of some rule
(403 for the others, e.g. ``operator``); the per-alert check follows. Each change is audited and
published (``live`` channel, ``alerts`` stream, ``live:alerts_open``). An acknowledged alert is
not escalated by the engine; an alert resolved while its condition still holds is reopened by the
engine on its next update (the condition is real), so "ack" is the action for an active alarm.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select, text

from qost_api.audit import audit
from qost_api.auth import AnyUser, Principal, require_config_roles
from qost_api.db import Session
from qost_api.deps import Config, PageDep, PlantClock, Settings, page_body, parse_instant
from qost_api.events_out import alert_message, open_alert_counts, publish_alert
from qost_api.problems import ProblemError
from twin_core.config import TwinConfig
from twin_core.db import AlertRow

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

ADMIN = "admin"


def alert_actor_roles(cfg: TwinConfig) -> set[str]:
    """Roles that may act on at least one alert: admin, recipients, escalation chains."""
    roles = {ADMIN} if ADMIN in cfg.rules.roles else set()
    for rule in cfg.rules.alert_rules:
        roles.update(rule.recipients)
    for level in cfg.rules.escalation.values():
        roles.update(level.chain)
    return roles


def actors_of(cfg: TwinConfig, rule_id: str, severity: str, escalation_level: int) -> set[str]:
    rule = cfg.alert_rules.get(rule_id)
    roles = {ADMIN}
    if rule is not None:
        roles.update(rule.recipients)
    chain = cfg.rules.escalation.get(severity)  # type: ignore[call-overload]
    if chain is not None and escalation_level > 0:
        roles.update(chain.chain[: escalation_level + 1])
    return roles


Actor = Annotated[Principal, Depends(require_config_roles(alert_actor_roles))]


def _view(r: Any, cfg: TwinConfig, principal: Principal) -> dict[str, Any]:
    rule = cfg.alert_rules.get(r["rule_id"])
    return {
        "id": r["id"],
        "ts": r["ts"].isoformat(),
        "rule_id": r["rule_id"],
        "severity": r["severity"],
        "entity_type": r["entity_type"],
        "entity": r["entity"],
        "title_ru": r["title_ru"],
        "title_kk": r["title_kk"],
        "message_ru": r["message_ru"],
        "message_kk": r["message_kk"],
        "value": r["value"],
        "status": r["status"],
        "ack_by": r["ack_by_name"],
        "ack_ts": r["ack_ts"].isoformat() if r["ack_ts"] else None,
        "resolved_ts": r["resolved_ts"].isoformat() if r["resolved_ts"] else None,
        "escalation_level": r["escalation_level"],
        "dedup_key": r["dedup_key"],
        "recipients": list(rule.recipients) if rule else [],
        "channels": list(rule.channels) if rule else [],
        "can_act": principal.role
        in actors_of(cfg, r["rule_id"], r["severity"], int(r["escalation_level"] or 0)),
    }


def _csv(values: Iterable[str] | None) -> list[str]:
    return [v.strip() for item in (values or []) for v in item.split(",") if v.strip()]


@router.get("", summary="Alerts, newest first (filters + cursor pagination)")
async def list_alerts(
    principal: AnyUser,
    cfg: Config,
    session: Session,
    page: PageDep,
    status: Annotated[
        list[str] | None, Query(description="open | ack | resolved (repeat/comma)")
    ] = None,
    severity: Annotated[list[str] | None, Query(description="info | warning | critical")] = None,
    rule_id: Annotated[list[str] | None, Query()] = None,
    entity: str | None = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    mine: Annotated[bool, Query(description="only alerts addressed to the caller's role")] = False,
) -> dict[str, Any]:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": page.limit + 1}
    for name, values, column in (
        ("status", _csv(status), "a.status"),
        ("severity", _csv(severity), "a.severity"),
        ("rules", _csv(rule_id), "a.rule_id"),
    ):
        if values:
            where.append(f"{column} = ANY(:{name})")
            params[name] = values
    if mine:
        own = [r.id for r in cfg.rules.alert_rules if principal.role in r.recipients]
        if principal.role != ADMIN:
            where.append("a.rule_id = ANY(:own)")
            params["own"] = own
    if entity:
        where.append("a.entity = :entity")
        params["entity"] = entity
    if start:
        where.append("a.ts >= :lo")
        params["lo"] = parse_instant(start, cfg)
    if end:
        where.append("a.ts < :hi")
        params["hi"] = parse_instant(end, cfg, end=True)
    if page.after is not None:
        where.append("(a.ts, a.id) < (:after_ts, :after_id)")
        params["after_ts"] = datetime.fromisoformat(page.after[0])
        params["after_id"] = page.after[1]
    sql = text(
        "SELECT a.*, u.username AS ack_by_name FROM alert a "
        "LEFT JOIN app_user u ON u.id = a.ack_by "
        f"WHERE {' AND '.join(where)} ORDER BY a.ts DESC, a.id DESC LIMIT :limit"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    body = page_body([_view(r, cfg, principal) for r in rows], page, "ts")
    body["open_counts"] = await open_alert_counts(session)
    return body


class AlertAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: Annotated[str | None, Field(max_length=500)] = None


async def _act(
    alert_id: int,
    action: Literal["ack", "resolve"],
    body: AlertAction | None,
    principal: Principal,
    request: Request,
    cfg: TwinConfig,
    now: datetime,
    settings: Any,
    session: Any,
) -> dict[str, Any]:
    row: AlertRow | None = await session.scalar(
        select(AlertRow).where(AlertRow.id == alert_id).with_for_update()
    )
    if row is None:
        raise ProblemError(404, "Not Found", f"alert {alert_id} does not exist", slug="not-found")
    allowed = actors_of(cfg, row.rule_id, row.severity, row.escalation_level)
    if principal.role not in allowed:
        raise ProblemError(
            403,
            "Forbidden",
            f"role '{principal.role}' is not a recipient of {row.rule_id} "
            f"(allowed: {', '.join(sorted(allowed))})",
            slug="forbidden",
        )
    before = {"status": row.status, "ack_ts": row.ack_ts.isoformat() if row.ack_ts else None}
    if row.status == "resolved":
        raise ProblemError(
            409, "Conflict", f"alert {alert_id} is already resolved", slug="alert-resolved"
        )
    if action == "ack":
        if row.status == "ack":
            return alert_message(row, cfg, ack_by=principal.username, changed=False)
        row.status = "ack"
        row.ack_by = principal.user_id
        row.ack_ts = now
    else:
        row.status = "resolved"
        row.resolved_ts = now
        if row.ack_ts is None:
            row.ack_by = principal.user_id
            row.ack_ts = now
    comment = body.comment if body is not None else None
    audit(
        session,
        ts=now,
        principal=principal,
        action=f"alert.{action}",
        entity_type="alert",
        entity_id=str(row.id),
        before=before,
        after={"status": row.status, "comment": comment, "dedup_key": row.dedup_key},
    )
    await session.commit()
    data = alert_message(row, cfg, ack_by=principal.username, changed=True)
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is not None:
        # the change is committed; without Redis the view catches up on the next engine commit
        with contextlib.suppress(OSError, ConnectionError):
            await publish_alert(
                redis, settings, ts=now, data=data, counts=await open_alert_counts(session)
            )
    return data


@router.post("/{alert_id}/ack", summary="Acknowledge an alert (stops escalation)")
async def ack_alert(
    alert_id: int,
    principal: Actor,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    settings: Settings,
    session: Session,
    body: AlertAction | None = None,
) -> dict[str, Any]:
    return await _act(
        alert_id, "ack", body, principal, request, cfg, clock.now(), settings, session
    )


@router.post("/{alert_id}/resolve", summary="Resolve an alert")
async def resolve_alert(
    alert_id: int,
    principal: Actor,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    settings: Settings,
    session: Session,
    body: AlertAction | None = None,
) -> dict[str, Any]:
    return await _act(
        alert_id, "resolve", body, principal, request, cfg, clock.now(), settings, session
    )
