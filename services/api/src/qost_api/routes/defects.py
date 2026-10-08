"""``GET /defects`` — defect records (units feed of the plant and operator entries); all roles."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from qost_api.auth import AnyUser
from qost_api.db import Session
from qost_api.deps import Config, PageDep, page_body, parse_instant

router = APIRouter(prefix="/api/v1", tags=["quality"])


@router.get("/defects", summary="Defects, newest first (filters + cursor pagination)")
async def list_defects(
    principal: AnyUser,
    cfg: Config,
    session: Session,
    page: PageDep,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    line: str | None = None,
    area: str | None = None,
    defect_code: str | None = None,
    source: Annotated[
        str | None, Query(description="sim | mqtt | opcua | operator | import")
    ] = None,
    body_id: str | None = None,
) -> dict[str, Any]:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": page.limit + 1}
    for name, value, column in (
        ("line", line, "d.line"),
        ("area", area, "d.area"),
        ("code", defect_code, "d.defect_code"),
        ("source", source, "d.source"),
        ("body", body_id, "d.body_id"),
    ):
        if value:
            where.append(f"{column} = :{name}")
            params[name] = value
    if start:
        where.append("d.ts >= :lo")
        params["lo"] = parse_instant(start, cfg)
    if end:
        where.append("d.ts < :hi")
        params["hi"] = parse_instant(end, cfg, end=True)
    if page.after is not None:
        where.append("(d.ts, d.id) < (:after_ts, :after_id)")
        params["after_ts"] = datetime.fromisoformat(page.after[0])
        params["after_id"] = page.after[1]
    sql = text(
        "SELECT d.*, u.username AS created_by_name FROM defect d "
        "LEFT JOIN app_user u ON u.id = d.created_by "
        f"WHERE {' AND '.join(where)} ORDER BY d.ts DESC, d.id DESC LIMIT :limit"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    items = []
    for r in rows:
        code = cfg.defects.get(r["defect_code"])
        items.append(
            {
                "id": r["id"],
                "ts": r["ts"].isoformat(),
                "line": r["line"],
                "area": r["area"],
                "equipment": r["equipment"],
                "body_id": r["body_id"],
                "defect_code": r["defect_code"],
                "defect_name_ru": code.name_ru if code else None,
                "qty": r["qty"],
                "disposition": r["disposition"],
                "source": r["source"],
                "created_by": r["created_by_name"],
            }
        )
    return page_body(items, page, "ts")
