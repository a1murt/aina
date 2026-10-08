"""``GET /downtime`` (all roles) and ``PATCH /downtime/{id}`` (master; operator — own line).

Classification (FR-ENG-03) never updates ``downtime`` directly: the engine is its single writer
(M3). The API audits the request and emits an ``operator`` event ``classify_downtime`` with
``{entity, start_ts, reason_code, comment?}`` into ``event_raw`` and the ``events`` stream; the
engine updates the unit and line rows, recomputes ``planned`` and writes new KPI versions of
closed shifts. The answer is ``202 Accepted`` with the row as it will look (``pending: true``).
Imported journal entries (no timestamps) are corrected by re-importing (409).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.audit import audit
from qost_api.auth import AnyUser, Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PageDep, PlantClock, Settings, page_body, parse_instant
from qost_api.events_out import operator_event, store_event, stream_event
from qost_api.problems import ProblemError
from twin_core.config import FALLBACK_REASON_CODE, TwinConfig

router = APIRouter(prefix="/api/v1", tags=["downtime"])
Classifier = Annotated[Principal, Depends(require_roles("master", "operator"))]

_COLUMNS = """
    d.id, d.entity, d.line, d.start_ts, d.end_ts, d.duration_s, d.planned, d.microstop,
    d.reason_code, d.reason_source, d.shift_date, d.shift_code, d.comment, d.classified_ts,
    d.import_id, COALESCE(d.start_ts, d.shift_date::timestamptz) AS sort_ts,
    (SELECT a.after->>'by' FROM audit_log a
     WHERE a.entity_type = 'downtime' AND a.entity_id = d.id::text
       AND a.action = 'downtime.classify'
     ORDER BY a.ts DESC, a.id DESC LIMIT 1) AS classified_by
"""


def downtime_view(r: Any, cfg: TwinConfig, now: datetime) -> dict[str, Any]:
    entity = r["entity"]
    end = r["end_ts"]
    start = r["start_ts"]
    duration = r["duration_s"]
    if duration is None and start is not None:
        duration = ((end or now) - start).total_seconds()
    age_min = (now - start).total_seconds() / 60.0 if start is not None else None
    reason = cfg.reasons.get(r["reason_code"])
    return {
        "id": r["id"],
        "entity": entity,
        "entity_type": "line" if entity in cfg.lines else "equipment",
        "line": r["line"],
        "area": cfg.area_of_line(r["line"]).code if r["line"] in cfg.lines else None,
        "start_ts": start.isoformat() if start else None,
        "end_ts": end.isoformat() if end else None,
        "open": start is not None and end is None,
        "duration_s": duration,
        "planned": r["planned"],
        "microstop": r["microstop"],
        "reason_code": r["reason_code"],
        "reason_name_ru": reason.name_ru if reason else None,
        "reason_source": r["reason_source"],
        "shift_date": r["shift_date"].isoformat() if r["shift_date"] else None,
        "shift_code": r["shift_code"],
        "comment": r["comment"],
        "classified_by": r["classified_by"],
        "classified_ts": r["classified_ts"].isoformat() if r["classified_ts"] else None,
        "import_id": r["import_id"],
        "needs_classification": bool(
            r["reason_code"] == FALLBACK_REASON_CODE
            and r["import_id"] is None
            and age_min is not None
            and age_min >= cfg.rules.engine.unclassified_after_min
        ),
        "sort_ts": r["sort_ts"].isoformat(),
    }


@router.get("/downtime", summary="Downtime records (events and imports), newest first")
async def list_downtime(
    principal: AnyUser,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    page: PageDep,
    line: Annotated[str | None, Query()] = None,
    entity: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query(pattern="^(line|equipment)$")] = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    open_only: Annotated[bool | None, Query(alias="open", description="only open stops")] = None,
    planned: bool | None = None,
    microstop: Annotated[
        bool | None, Query(description="default: microstops are excluded")
    ] = False,
    reason_code: str | None = None,
    reason_source: Annotated[str | None, Query(pattern="^(auto|operator|import)$")] = None,
    needs_classification: bool | None = None,
) -> dict[str, Any]:
    now = clock.now()
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": page.limit + 1}
    if line:
        where.append("d.line = :line")
        params["line"] = line
    if entity:
        where.append("d.entity = :entity")
        params["entity"] = entity
    if entity_type:
        where.append(
            "d.entity = ANY(:lines)" if entity_type == "line" else "NOT (d.entity = ANY(:lines))"
        )
        params["lines"] = list(cfg.lines)
    if start:
        where.append("COALESCE(d.end_ts, :now) >= :lo")
        params["lo"] = parse_instant(start, cfg)
        params["now"] = now
    if end:
        where.append("COALESCE(d.start_ts, d.shift_date::timestamptz) < :hi")
        params["hi"] = parse_instant(end, cfg, end=True)
    if open_only:
        where.append("d.start_ts IS NOT NULL AND d.end_ts IS NULL")
    if planned is not None:
        where.append("d.planned = :planned")
        params["planned"] = planned
    if microstop is not None:
        where.append("d.microstop = :microstop")
        params["microstop"] = microstop
    if reason_code:
        where.append("d.reason_code = :reason")
        params["reason"] = reason_code
    if reason_source:
        where.append("d.reason_source = :source")
        params["source"] = reason_source
    if needs_classification:
        where.append("d.reason_code = :unk AND d.import_id IS NULL AND d.start_ts <= :flag_before")
        params["unk"] = FALLBACK_REASON_CODE
        params["flag_before"] = now - timedelta(minutes=cfg.rules.engine.unclassified_after_min)
    if page.after is not None:
        where.append(
            "(COALESCE(d.start_ts, d.shift_date::timestamptz), d.id) < (:after_ts, :after_id)"
        )
        params["after_ts"] = datetime.fromisoformat(page.after[0])
        params["after_id"] = page.after[1]
    sql = text(
        f"SELECT {_COLUMNS} FROM downtime d WHERE {' AND '.join(where)} "
        "ORDER BY sort_ts DESC, d.id DESC LIMIT :limit"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return page_body([downtime_view(r, cfg, now) for r in rows], page, "sort_ts")


class ClassifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: Annotated[str, Field(min_length=1)]
    comment: Annotated[str | None, Field(max_length=500)] = None


async def get_downtime_row(session: AsyncSession, downtime_id: int) -> Any:
    sql = text(f"SELECT {_COLUMNS} FROM downtime d WHERE d.id = :id")
    return (await session.execute(sql, {"id": downtime_id})).mappings().first()


@router.patch(
    "/downtime/{downtime_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Classify a stop (reason + comment); applied by the engine (FR-ENG-03)",
    responses={409: {"description": "Imported journal entry"}},
)
async def classify_downtime(
    downtime_id: int,
    body: ClassifyBody,
    principal: Classifier,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    settings: Settings,
    session: Session,
) -> JSONResponse:
    reason = cfg.reasons.get(body.reason_code)
    if reason is None:
        raise ProblemError(
            422,
            "Request validation failed",
            f"unknown reason '{body.reason_code}'",
            slug="validation",
            errors=[
                {"loc": ["body", "reason_code"], "msg": "unknown reason", "type": "value_error"}
            ],
        )
    row = await get_downtime_row(session, downtime_id)
    if row is None:
        raise ProblemError(
            404, "Not Found", f"downtime {downtime_id} does not exist", slug="not-found"
        )
    if not principal.may_act_on_line(row["line"]):
        raise ProblemError(
            403,
            "Forbidden",
            f"operator of {', '.join(principal.lines) or 'no line'} may not classify {row['line']}",
            slug="forbidden",
        )
    if row["import_id"] is not None or row["start_ts"] is None:
        raise ProblemError(
            409,
            "Conflict",
            "imported journal entries are corrected by importing the file again",
            slug="imported-downtime",
        )
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        raise ProblemError(
            503, "Live store is not configured", "Redis is disabled", slug="no-redis"
        )
    now = clock.now()
    payload: dict[str, Any] = {
        "entity": row["entity"],
        "start_ts": row["start_ts"].isoformat(),
        "reason_code": body.reason_code,
        "downtime_id": downtime_id,
    }
    if body.comment:
        payload["comment"] = body.comment
    event = operator_event(
        cfg,
        principal,
        ts=now,
        action="classify_downtime",
        entity_type="line" if row["entity"] in cfg.lines else "equipment",
        entity=row["entity"],
        payload=payload,
    )
    await store_event(session, event)
    audit(
        session,
        ts=now,
        principal=principal,
        action="downtime.classify",
        entity_type="downtime",
        entity_id=str(downtime_id),
        before={
            "reason_code": row["reason_code"],
            "reason_source": row["reason_source"],
            "planned": row["planned"],
            "comment": row["comment"],
        },
        after={
            "reason_code": body.reason_code,
            "planned": reason.planned,
            "comment": body.comment,
            "event_id": event.event_id,
        },
    )
    await session.flush()
    await stream_event(redis, settings, event)
    await session.commit()
    view = downtime_view(row, cfg, now)
    view.update(
        {
            "reason_code": body.reason_code,
            "reason_name_ru": reason.name_ru,
            "reason_source": "operator",
            "planned": reason.planned,
            "comment": body.comment or row["comment"],
            "classified_by": principal.username,
            "needs_classification": False,
            "pending": True,
            "event_id": event.event_id,
        }
    )
    return JSONResponse(view, status_code=status.HTTP_202_ACCEPTED)
