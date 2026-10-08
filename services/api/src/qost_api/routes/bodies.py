"""``GET /bodies/{body_id}`` — trace of one body through the lines (SPEC §12.2, P1).

Roles: quality, master. Built from ``unit_event`` (every exit of a line) and ``defect``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config
from qost_api.problems import ProblemError
from twin_core.events import FIRST_EXIT_RESULTS

router = APIRouter(prefix="/api/v1", tags=["quality"])
Tracer = Annotated[Principal, Depends(require_roles("quality", "master"))]


@router.get("/bodies/{body_id}", summary="Trace of a body: line exits, defects, final status")
async def get_body(
    body_id: str, principal: Tracer, cfg: Config, session: Session
) -> dict[str, Any]:
    events = (
        (
            await session.execute(
                text(
                    "SELECT ts, line, product, result, defect_code FROM unit_event "
                    "WHERE body_id = :id ORDER BY ts"
                ),
                {"id": body_id},
            )
        )
        .mappings()
        .all()
    )
    defects = (
        (
            await session.execute(
                text(
                    "SELECT id, ts, line, area, equipment, defect_code, qty, disposition, source "
                    "FROM defect WHERE body_id = :id ORDER BY ts, id"
                ),
                {"id": body_id},
            )
        )
        .mappings()
        .all()
    )
    if not events and not defects:
        raise ProblemError(404, "Not Found", f"no records for body '{body_id}'", slug="not-found")

    def code_name(code: str | None) -> str | None:
        item = cfg.defects.get(code) if code else None
        return item.name_ru if item else None

    path = [
        {
            "ts": e["ts"].isoformat(),
            "line": e["line"],
            "line_name_ru": cfg.lines[e["line"]].name_ru if e["line"] in cfg.lines else None,
            "result": e["result"],
            "first_exit": e["result"] in FIRST_EXIT_RESULTS,
            "defect_code": e["defect_code"],
            "defect_name_ru": code_name(e["defect_code"]),
        }
        for e in events
    ]
    last_line = cfg.flow_lines[-1]
    good_exit = any(e["line"] == last_line and e["result"] == "pass" for e in events)
    if any(e["result"] == "scrap" for e in events):
        status = "scrap"
    elif good_exit:
        status = "finished"
    elif events and events[-1]["result"] in ("defect", "rework_pass"):
        status = "in_rework" if events[-1]["result"] == "defect" else "in_process"
    else:
        status = "in_process"
    return {
        "body_id": body_id,
        "product": next((e["product"] for e in events if e["product"]), None),
        "first_ts": path[0]["ts"] if path else defects[0]["ts"].isoformat(),
        "last_ts": path[-1]["ts"] if path else defects[-1]["ts"].isoformat(),
        "status": status,
        "lines_passed": [e["line"] for e in events if e["result"] in ("pass", "rework_pass")],
        "events": path,
        "defects": [
            {
                "id": d["id"],
                "ts": d["ts"].isoformat(),
                "line": d["line"],
                "area": d["area"],
                "equipment": d["equipment"],
                "defect_code": d["defect_code"],
                "defect_name_ru": code_name(d["defect_code"]),
                "qty": d["qty"],
                "disposition": d["disposition"],
                "source": d["source"],
            }
            for d in defects
        ],
    }
