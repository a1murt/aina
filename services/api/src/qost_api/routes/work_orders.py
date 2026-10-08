"""``/work-orders`` — maintenance work orders (SPEC §8 ``work_order``, §12.2, US-5/US-6).

Roles: maintenance, master, admin. ``POST`` with an ``alert_id`` pre-fills the order from the
alert (unit, kind, priority, title, description and the due time: the recommended shift change of
AL-M2, the next shift change for AL-M1); explicit fields win over the prefill. One active order per
alert (409 ``work-order-exists`` with the existing id). Every change is audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from qost_api.audit import audit
from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PageDep, PlantClock, page_body, parse_instant
from qost_api.problems import ProblemError
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.db import WorkOrder

router = APIRouter(prefix="/api/v1/work-orders", tags=["maintenance"])
Maintainer = Annotated[Principal, Depends(require_roles("maintenance", "master", "admin"))]

Kind = Literal["corrective", "preventive", "predictive"]
Priority = Literal["low", "normal", "high", "urgent"]
Status = Literal["open", "in_progress", "done", "cancelled"]
ACTIVE = ("open", "in_progress")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("in_progress", "done", "cancelled"),
    "in_progress": ("open", "done", "cancelled"),
    "done": (),
    "cancelled": (),
}
PREDICTIVE_RULES = ("AL-M1", "AL-M2")
SEVERITY_PRIORITY = {"info": "low", "warning": "normal", "critical": "urgent"}

_COLUMNS = (
    "w.id, w.equipment, w.alert_id, w.kind, w.priority, w.status, w.title, w.description, "
    "w.assignee, w.created_by, w.created_ts, w.due_ts, w.closed_ts, "
    "ua.username AS assignee_name, uc.username AS created_by_name, "
    "a.rule_id AS alert_rule, a.severity AS alert_severity"
)
_FROM = (
    "FROM work_order w LEFT JOIN app_user ua ON ua.id = w.assignee "
    "LEFT JOIN app_user uc ON uc.id = w.created_by LEFT JOIN alert a ON a.id = w.alert_id"
)


class WorkOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    equipment: str | None = None
    alert_id: int | None = Field(default=None, ge=1)
    kind: Kind | None = None
    priority: Priority | None = None
    title: Annotated[str | None, Field(max_length=200)] = None
    description: Annotated[str | None, Field(max_length=4000)] = None
    assignee: Annotated[str | None, Field(description="username")] = None
    due_ts: datetime | None = None


class WorkOrderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status | None = None
    priority: Priority | None = None
    title: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: Annotated[str | None, Field(max_length=4000)] = None
    assignee: Annotated[str | None, Field(description="username; empty string unassigns")] = None
    due_ts: datetime | None = None


def prefill_from_alert(cfg: TwinConfig, alert: Any, now: datetime) -> dict[str, Any]:
    """Work-order fields suggested by an alert row (``alert`` mapping)."""
    if alert["entity_type"] != "equipment" or alert["entity"] not in cfg.equipment:
        raise ProblemError(
            422,
            "Request validation failed",
            f"alert {alert['id']} is not about a unit (entity '{alert['entity']}')",
            slug="validation",
        )
    code = alert["entity"]
    name = cfg.equipment[code].name_ru
    rule = alert["rule_id"]
    value = alert["value"] if isinstance(alert["value"], dict) else {}
    kind: Kind = "predictive" if rule in PREDICTIVE_RULES else "corrective"
    priority = SEVERITY_PRIORITY.get(alert["severity"], "normal")
    if rule == "AL-M1" and priority == "normal":
        priority = "high"
    due: datetime | None = None
    if rule == "AL-M1":
        pct = round(float(value.get("p_failure", 0.0)) * 100)
        title = f"Проверить {name}: риск отказа {pct}% в ближайшие {value.get('horizon_h', 8):g} ч"
        due = cfg.calendar.next_shift_change(now)
    elif rule == "AL-M2":
        signal = str(value.get("signal_name_ru") or value.get("signal") or "сигнал").lower()
        title = f"Обслужить {name}: {signal} приближается к пределу"
        window = value.get("window") or value.get("limit_at")
        due = ensure_utc(datetime.fromisoformat(str(window))) if window else now
    elif rule == "AL-S1":
        title = f"Устранить остановку: {name}"
    else:
        title = f"{alert['title_ru']}: {name}"
    return {
        "equipment": code,
        "kind": kind,
        "priority": priority,
        "title": title,
        "description": alert["message_ru"],
        "due_ts": due,
    }


def work_order_view(r: Any, cfg: TwinConfig, now: datetime) -> dict[str, Any]:
    eq = cfg.equipment.get(r["equipment"])
    due = r["due_ts"]
    return {
        "id": r["id"],
        "equipment": r["equipment"],
        "equipment_name_ru": eq.name_ru if eq else None,
        "line": cfg.line_of_equipment(r["equipment"]).code if eq else None,
        "alert_id": r["alert_id"],
        "alert_rule": r["alert_rule"],
        "kind": r["kind"],
        "priority": r["priority"],
        "status": r["status"],
        "title": r["title"],
        "description": r["description"],
        "assignee": r["assignee_name"],
        "created_by": r["created_by_name"],
        "created_ts": r["created_ts"].isoformat(),
        "due_ts": due.isoformat() if due else None,
        "closed_ts": r["closed_ts"].isoformat() if r["closed_ts"] else None,
        "overdue": bool(due and r["status"] in ACTIVE and due < now),
    }


async def _fetch(session: Any, order_id: int) -> Any:
    result = await session.execute(
        text(f"SELECT {_COLUMNS} {_FROM} WHERE w.id = :id"), {"id": order_id}
    )
    return result.mappings().first()


async def _user_id(session: Any, username: str) -> int:
    row = (
        await session.execute(
            text("SELECT id FROM app_user WHERE username = :u AND active"), {"u": username}
        )
    ).first()
    if row is None:
        raise ProblemError(
            422,
            "Request validation failed",
            f"unknown or inactive user '{username}'",
            slug="validation",
            errors=[{"loc": ["body", "assignee"], "msg": "unknown user", "type": "value_error"}],
        )
    return int(row[0])


def _snapshot(r: Any) -> dict[str, Any]:
    return {
        "status": r["status"],
        "priority": r["priority"],
        "title": r["title"],
        "assignee": r["assignee_name"],
        "due_ts": r["due_ts"].isoformat() if r["due_ts"] else None,
    }


@router.get("", summary="Work orders, newest first (filters + cursor pagination)")
async def list_work_orders(
    principal: Maintainer,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    page: PageDep,
    state: Annotated[list[str] | None, Query(alias="status", description="repeat/comma")] = None,
    equipment: str | None = None,
    kind: Kind | None = None,
    alert_id: int | None = None,
    assignee: str | None = None,
    mine: Annotated[bool, Query(description="assigned to the caller")] = False,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": page.limit + 1}
    states = [s.strip() for item in (state or []) for s in item.split(",") if s.strip()]
    bad = [s for s in states if s not in TRANSITIONS]
    if bad:
        raise ProblemError(
            422, "Request validation failed", f"unknown status {', '.join(bad)}", slug="validation"
        )
    if states:
        where.append("w.status = ANY(:states)")
        params["states"] = states
    for name, value, column in (
        ("equipment", equipment, "w.equipment"),
        ("kind", kind, "w.kind"),
        ("alert_id", alert_id, "w.alert_id"),
        ("assignee", assignee, "ua.username"),
    ):
        if value is not None:
            where.append(f"{column} = :{name}")
            params[name] = value
    if mine:
        where.append("ua.username = :me")
        params["me"] = principal.username
    if start:
        where.append("w.created_ts >= :lo")
        params["lo"] = parse_instant(start, cfg)
    if end:
        where.append("w.created_ts < :hi")
        params["hi"] = parse_instant(end, cfg, end=True)
    if page.after is not None:
        where.append("(w.created_ts, w.id) < (:after_ts, :after_id)")
        params["after_ts"] = datetime.fromisoformat(page.after[0])
        params["after_id"] = page.after[1]
    sql = text(
        f"SELECT {_COLUMNS} {_FROM} WHERE {' AND '.join(where)} "
        "ORDER BY w.created_ts DESC, w.id DESC LIMIT :limit"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    now = clock.now()
    return page_body([work_order_view(r, cfg, now) for r in rows], page, "created_ts")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a work order (from an alert with prefill, or free-form)",
    responses={409: {"description": "The alert already has an active work order"}},
)
async def create_work_order(
    body: WorkOrderCreate, principal: Maintainer, cfg: Config, clock: PlantClock, session: Session
) -> dict[str, Any]:
    now = clock.now()
    fields: dict[str, Any] = {}
    if body.alert_id is not None:
        alert = (
            (
                await session.execute(
                    text(
                        "SELECT id, rule_id, severity, entity_type, entity, title_ru, "
                        "message_ru, value FROM alert WHERE id = :id"
                    ),
                    {"id": body.alert_id},
                )
            )
            .mappings()
            .first()
        )
        if alert is None:
            raise ProblemError(
                404, "Not Found", f"alert {body.alert_id} does not exist", slug="not-found"
            )
        active = (
            await session.execute(
                text(
                    "SELECT id FROM work_order WHERE alert_id = :id "
                    "AND status IN ('open', 'in_progress')"
                ),
                {"id": body.alert_id},
            )
        ).first()
        if active is not None:
            raise ProblemError(
                409,
                "Conflict",
                f"alert {body.alert_id} already has the active work order {active[0]}",
                slug="work-order-exists",
                work_order_id=int(active[0]),
            )
        fields = prefill_from_alert(cfg, alert, now)
    explicit = body.model_dump(exclude_none=True, exclude={"alert_id", "assignee"})
    fields.update(explicit)
    if "equipment" not in fields:
        raise ProblemError(
            422,
            "Request validation failed",
            "give 'equipment' or an 'alert_id' of a unit alert",
            slug="validation",
        )
    if fields["equipment"] not in cfg.equipment:
        raise ProblemError(
            404, "Not Found", f"unknown equipment '{fields['equipment']}'", slug="not-found"
        )
    if "title" not in fields:
        raise ProblemError(
            422, "Request validation failed", "'title' is required", slug="validation"
        )
    assignee_id = await _user_id(session, body.assignee) if body.assignee else None
    order = WorkOrder(
        equipment=fields["equipment"],
        alert_id=body.alert_id,
        kind=fields.get("kind", "corrective"),
        priority=fields.get("priority", "normal"),
        status="open",
        title=fields["title"],
        description=fields.get("description"),
        assignee=assignee_id,
        created_by=principal.user_id,
        created_ts=now,
        due_ts=ensure_utc(fields["due_ts"]) if fields.get("due_ts") else None,
    )
    session.add(order)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ProblemError(
            409, "Conflict", "the alert already has an active work order", slug="work-order-exists"
        ) from None
    row = await _fetch(session, order.id)
    audit(
        session,
        ts=now,
        principal=principal,
        action="work_order.create",
        entity_type="work_order",
        entity_id=str(order.id),
        after={**_snapshot(row), "equipment": order.equipment, "alert_id": body.alert_id},
    )
    await session.commit()
    return work_order_view(row, cfg, now)


@router.patch("/{order_id}", summary="Update a work order (status, assignee, due time, ...)")
async def patch_work_order(
    order_id: int,
    body: WorkOrderPatch,
    principal: Maintainer,
    cfg: Config,
    clock: PlantClock,
    session: Session,
) -> dict[str, Any]:
    row = await _fetch(session, order_id)
    if row is None:
        raise ProblemError(
            404, "Not Found", f"work order {order_id} does not exist", slug="not-found"
        )
    now = clock.now()
    changes = body.model_dump(exclude_unset=True)
    if row["status"] not in ACTIVE and changes:
        raise ProblemError(
            409,
            "Conflict",
            f"work order {order_id} is {row['status']} and cannot be changed",
            slug="work-order-closed",
        )
    new_status = changes.get("status")
    if (
        new_status is not None
        and new_status != row["status"]
        and new_status not in TRANSITIONS[row["status"]]
    ):
        raise ProblemError(
            409,
            "Conflict",
            f"cannot go from {row['status']} to {new_status}",
            slug="work-order-transition",
        )
    sets: dict[str, Any] = {}
    for key in ("priority", "title", "description"):
        if key in changes and changes[key] is not None:
            sets[key] = changes[key]
    if "due_ts" in changes and changes["due_ts"] is not None:
        sets["due_ts"] = ensure_utc(changes["due_ts"])
    if "assignee" in changes:
        sets["assignee"] = (
            await _user_id(session, changes["assignee"]) if changes["assignee"] else None
        )
    if new_status is not None:
        sets["status"] = new_status
        if new_status in ("done", "cancelled"):
            sets["closed_ts"] = now
    if sets:
        assignments = ", ".join(f"{k} = :{k}" for k in sets)
        await session.execute(
            text(f"UPDATE work_order SET {assignments} WHERE id = :id"), {**sets, "id": order_id}
        )
        after = await _fetch(session, order_id)
        audit(
            session,
            ts=now,
            principal=principal,
            action="work_order.update",
            entity_type="work_order",
            entity_id=str(order_id),
            before=_snapshot(row),
            after=_snapshot(after),
        )
        await session.commit()
        row = after
    return work_order_view(row, cfg, now)
