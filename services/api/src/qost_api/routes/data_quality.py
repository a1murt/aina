"""``GET /data-quality``, ``POST /data-quality/{id}/resolve`` — admin, director, quality (§9.6)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from qost_api.audit import audit
from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PageDep, PlantClock, page_body
from qost_api.problems import ProblemError
from twin_core.config import TwinConfig
from twin_core.db import DqIssueRow

router = APIRouter(prefix="/api/v1/data-quality", tags=["data-quality"])
DQ_ROLES = ("admin", "director", "quality")
Reviewer = Annotated[Principal, Depends(require_roles(*DQ_ROLES))]


def _view(r: Any, cfg: TwinConfig) -> dict[str, Any]:
    rule = cfg.dq_rules.get(r["rule_id"])
    return {
        "id": r["id"],
        "ts": r["ts"].isoformat(),
        "rule_id": r["rule_id"],
        "rule_name_ru": rule.name_ru if rule else None,
        "rule_name_kk": rule.name_kk if rule else None,
        "severity": r["severity"],
        "entity": r["entity"],
        "period_date": r["period_date"].isoformat() if r["period_date"] else None,
        "details": r["details"],
        "status": r["status"],
        "resolved_by": r["resolved_by_name"],
        "comment": r["comment"],
        "import_id": r["import_id"],
        "source": "import" if r["import_id"] is not None else "live",
    }


@router.get("", summary="Data-quality findings (DQ-01…07), newest first")
async def list_dq(
    principal: Reviewer,
    cfg: Config,
    session: Session,
    page: PageDep,
    status: Annotated[str | None, Query(pattern="^(open|resolved)$")] = None,
    rule_id: str | None = None,
    entity: str | None = None,
    import_id: int | None = None,
    start: Annotated[
        str | None, Query(alias="from", description="period date from (YYYY-MM-DD)")
    ] = None,
    end: Annotated[str | None, Query(alias="to", description="period date to, inclusive")] = None,
) -> dict[str, Any]:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": page.limit + 1}
    for name, value, column in (
        ("status", status, "q.status"),
        ("rule", rule_id, "q.rule_id"),
        ("entity", entity, "q.entity"),
        ("import_id", import_id, "q.import_id"),
    ):
        if value is not None:
            where.append(f"{column} = :{name}")
            params[name] = value
    try:
        if start:
            where.append("q.period_date >= :d0")
            params["d0"] = datetime.fromisoformat(start).date()
        if end:
            where.append("q.period_date <= :d1")
            params["d1"] = datetime.fromisoformat(end).date()
    except ValueError:
        raise ProblemError(
            422, "Request validation failed", "dates must be YYYY-MM-DD", slug="validation"
        ) from None
    if page.after is not None:
        where.append("(q.ts, q.id) < (:after_ts, :after_id)")
        params["after_ts"] = datetime.fromisoformat(page.after[0])
        params["after_id"] = page.after[1]
    sql = text(
        "SELECT q.*, u.username AS resolved_by_name FROM dq_issue q "
        "LEFT JOIN app_user u ON u.id = q.resolved_by "
        f"WHERE {' AND '.join(where)} ORDER BY q.ts DESC, q.id DESC LIMIT :limit"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return page_body([_view(r, cfg) for r in rows], page, "ts")


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: Annotated[str | None, Field(max_length=1000)] = None


@router.post("/{issue_id}/resolve", summary="Close a finding with a comment")
async def resolve_dq(
    issue_id: int,
    principal: Reviewer,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    body: ResolveBody | None = None,
) -> dict[str, Any]:
    row: DqIssueRow | None = await session.scalar(
        select(DqIssueRow).where(DqIssueRow.id == issue_id).with_for_update()
    )
    if row is None:
        raise ProblemError(404, "Not Found", f"finding {issue_id} does not exist", slug="not-found")
    if row.status == "resolved":
        raise ProblemError(
            409, "Conflict", f"finding {issue_id} is already resolved", slug="dq-resolved"
        )
    now = clock.now()
    comment = body.comment if body is not None else None
    audit(
        session,
        ts=now,
        principal=principal,
        action="dq.resolve",
        entity_type="dq_issue",
        entity_id=str(issue_id),
        before={"status": row.status, "comment": row.comment},
        after={"status": "resolved", "comment": comment},
    )
    row.status = "resolved"
    row.resolved_by = principal.user_id
    row.comment = comment
    await session.commit()
    rule = cfg.dq_rules.get(row.rule_id)
    return {
        "id": row.id,
        "rule_id": row.rule_id,
        "rule_name_ru": rule.name_ru if rule else None,
        "entity": row.entity,
        "status": row.status,
        "resolved_by": principal.username,
        "comment": row.comment,
    }
