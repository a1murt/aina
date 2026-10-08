"""Operator terminal actions (SPEC §12.2: operator — own line, master — any line; §13.2, US-3).

* ``POST /operator/andon`` — operator event ``andon`` + alert AL-A1 "вызов мастера" (one open
  call per line; pressing again counts up) so the master sees it live without reloading.
* ``POST /operator/defects`` — a ``defect`` row (``source=operator``) + operator event
  ``log_defect`` (quality screens, SPC and a pilot without the MQTT units feed use it).
* ``POST /operator/material-call`` — operator event ``material_call`` + alert AL-A2.

Every action is one transaction with its ``audit_log`` row; the event also goes to the ``events``
stream (engine) before the commit and alert changes are published on ``live``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.audit import audit
from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, Settings
from qost_api.events_out import (
    alert_message,
    open_alert_counts,
    operator_event,
    publish_alert,
    store_event,
    stream_event,
)
from qost_api.problems import ProblemError
from twin_core.alert_text import alert_message_ru
from twin_core.config import ANY_AREA, TwinConfig
from twin_core.db import AlertRow, Defect
from twin_core.events import OperatorAction
from twin_core.rules import AL_ANDON, AL_MATERIAL_CALL, Alert

router = APIRouter(prefix="/api/v1/operator", tags=["operator"])
Terminal = Annotated[Principal, Depends(require_roles("operator", "master"))]


class AndonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line: str
    equipment: str | None = None
    reason_code: str | None = None
    comment: Annotated[str | None, Field(max_length=500)] = None


class DefectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line: str
    defect_code: str
    qty: Annotated[int, Field(ge=1, le=100)] = 1
    body_id: Annotated[str | None, Field(max_length=64)] = None
    equipment: str | None = None
    comment: Annotated[str | None, Field(max_length=500)] = None


class MaterialCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line: str
    product: str | None = None
    comment: Annotated[str | None, Field(max_length=500)] = None


def _invalid(field: str, msg: str) -> ProblemError:
    return ProblemError(
        422,
        "Request validation failed",
        msg,
        slug="validation",
        errors=[{"loc": ["body", field], "msg": msg, "type": "value_error"}],
    )


def check_line(
    cfg: TwinConfig, principal: Principal, line: str, equipment: str | None = None
) -> None:
    if line not in cfg.lines:
        raise _invalid("line", f"unknown line '{line}' (known: {', '.join(cfg.lines)})")
    if not principal.may_act_on_line(line):
        raise ProblemError(
            403,
            "Forbidden",
            f"operator of {', '.join(principal.lines) or 'no line'} may not act on {line}",
            slug="forbidden",
        )
    if equipment is not None and equipment not in {e.code for e in cfg.lines[line].equipment}:
        raise _invalid("equipment", f"'{equipment}' is not equipment of {line}")


def _redis(request: Request) -> Redis:
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        raise ProblemError(
            503, "Live store is not configured", "Redis is disabled", slug="no-redis"
        )
    return redis


async def _raise_call(
    session: AsyncSession,
    cfg: TwinConfig,
    principal: Principal,
    *,
    rule_id: str,
    line: str,
    value: dict[str, Any],
    now: Any,
) -> AlertRow:
    """Open (or count up) the operator-call alert of a line."""
    rule = cfg.alert_rules.get(rule_id)
    if rule is None:
        raise ProblemError(
            503, "Rule is disabled", f"{rule_id} is not in rules.yaml", slug="rule-disabled"
        )
    severity = rule.severity if isinstance(rule.severity, str) else "warning"
    existing: AlertRow | None = await session.scalar(
        select(AlertRow)
        .where(
            AlertRow.rule_id == rule_id,
            AlertRow.entity == line,
            AlertRow.status.in_(("open", "ack")),
        )
        .order_by(AlertRow.ts.desc())
        .limit(1)
        .with_for_update()
    )
    if existing is not None:
        previous = existing.value if isinstance(existing.value, dict) else {}
        value = {
            **value,
            "count": int(previous.get("count", 1) or 1) + 1,
            "first_ts": previous.get("first_ts"),
        }
    else:
        value = {**value, "count": 1, "first_ts": now.isoformat()}
    finding = Alert(rule_id, severity, "line", line, now.astimezone(cfg.timezone).date(), value)
    message = alert_message_ru(finding, cfg)
    if existing is not None:
        existing.value = value
        existing.message_ru = message
        existing.ts = now
        await session.flush()
        return existing
    row = AlertRow(
        ts=now,
        rule_id=rule_id,
        severity=severity,
        entity_type="line",
        entity=line,
        title_ru=rule.name_ru,
        title_kk=rule.name_kk,
        message_ru=message,
        message_kk=None,
        value=value,
        status="open",
        escalation_level=0,
        dedup_key=f"{rule_id}|{line}|{now.isoformat()}",
    )
    session.add(row)
    await session.flush()
    return row


async def _call(
    request: Request,
    session: AsyncSession,
    cfg: TwinConfig,
    principal: Principal,
    settings: Any,
    now: Any,
    *,
    action: OperatorAction,
    rule_id: str,
    line: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    redis = _redis(request)
    event = operator_event(
        cfg, principal, ts=now, action=action, entity_type="line", entity=line, payload=payload
    )
    await store_event(session, event)
    alert = await _raise_call(
        session,
        cfg,
        principal,
        rule_id=rule_id,
        line=line,
        value={**payload, "user": principal.username, "event_id": event.event_id},
        now=now,
    )
    audit(
        session,
        ts=now,
        principal=principal,
        action=f"operator.{action}",
        entity_type="line",
        entity_id=line,
        after={**payload, "event_id": event.event_id, "alert_id": alert.id},
    )
    await session.flush()
    await stream_event(redis, settings, event)
    await session.commit()
    data = alert_message(alert, cfg, notify=list(cfg.alert_rules[rule_id].recipients))
    await publish_alert(redis, settings, ts=now, data=data, counts=await open_alert_counts(session))
    return {"event_id": event.event_id, "alert": data}


@router.post("/andon", status_code=status.HTTP_201_CREATED, summary="Andon: call the master (US-3)")
async def andon(
    body: AndonBody,
    principal: Terminal,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    settings: Settings,
    session: Session,
) -> dict[str, Any]:
    check_line(cfg, principal, body.line, body.equipment)
    if body.reason_code is not None and body.reason_code not in cfg.reasons:
        raise _invalid("reason_code", f"unknown reason '{body.reason_code}'")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    return await _call(
        request,
        session,
        cfg,
        principal,
        settings,
        clock.now(),
        action="andon",
        rule_id=AL_ANDON,
        line=body.line,
        payload=payload,
    )


@router.post(
    "/material-call", status_code=status.HTTP_201_CREATED, summary="No components: call logistics"
)
async def material_call(
    body: MaterialCallBody,
    principal: Terminal,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    settings: Settings,
    session: Session,
) -> dict[str, Any]:
    check_line(cfg, principal, body.line)
    if body.product is not None and body.product not in cfg.products:
        raise _invalid("product", f"unknown product '{body.product}'")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    return await _call(
        request,
        session,
        cfg,
        principal,
        settings,
        clock.now(),
        action="material_call",
        rule_id=AL_MATERIAL_CALL,
        line=body.line,
        payload=payload,
    )


@router.post("/defects", status_code=status.HTTP_201_CREATED, summary="Record a defect")
async def log_defect(
    body: DefectBody,
    principal: Terminal,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    settings: Settings,
    session: Session,
) -> dict[str, Any]:
    check_line(cfg, principal, body.line, body.equipment)
    area = cfg.area_of_line(body.line).code
    code = cfg.defects.get(body.defect_code)
    if code is None or code.area not in (area, ANY_AREA):
        raise _invalid("defect_code", f"'{body.defect_code}' is not a defect code of area {area}")
    redis = _redis(request)
    now = clock.now()
    row = Defect(
        ts=now,
        line=body.line,
        area=area,
        equipment=body.equipment,
        body_id=body.body_id,
        defect_code=body.defect_code,
        qty=body.qty,
        disposition=code.disposition,
        source="operator",
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    payload: dict[str, Any] = {
        "line": body.line,
        "defect_code": body.defect_code,
        "qty": body.qty,
        "disposition": code.disposition,
        "defect_id": row.id,
    }
    for key in ("body_id", "equipment", "comment"):
        if getattr(body, key) is not None:
            payload[key] = getattr(body, key)
    event = operator_event(
        cfg,
        principal,
        ts=now,
        action="log_defect",
        entity_type="line",
        entity=body.line,
        payload=payload,
    )
    await store_event(session, event)
    audit(
        session,
        ts=now,
        principal=principal,
        action="defect.create",
        entity_type="defect",
        entity_id=str(row.id),
        after={**payload, "event_id": event.event_id},
    )
    await session.flush()
    await stream_event(redis, settings, event)
    await session.commit()
    return {
        "id": row.id,
        "ts": now.isoformat(),
        "line": body.line,
        "area": area,
        "equipment": body.equipment,
        "body_id": body.body_id,
        "defect_code": body.defect_code,
        "defect_name_ru": code.name_ru,
        "qty": body.qty,
        "disposition": code.disposition,
        "source": "operator",
        "created_by": principal.username,
        "event_id": event.event_id,
    }
