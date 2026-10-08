"""``GET /predictions`` — PdM history of the engine (SPEC §8 ``prediction``, §11.1, §12.2).

Roles: maintenance, director, admin. ``latest=true`` returns the newest prediction of every unit
(the maintenance list); otherwise the history of the requested units, newest first. The cursor is
opaque (keyset on time and unit).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, parse_instant
from qost_api.problems import ProblemError
from twin_core.config import TwinConfig

router = APIRouter(prefix="/api/v1", tags=["maintenance"])
Maintainer = Annotated[Principal, Depends(require_roles("maintenance", "director", "admin"))]
DEFAULT_SPAN = timedelta(hours=24)
MAX_LIMIT = 1000


def factor_texts(top_factors: Any, lang: str) -> list[str]:
    """Human-readable top factors (``text_ru`` / ``text_kk`` stored by the engine)."""
    key = "text_kk" if lang == "kk" else "text_ru"
    out: list[str] = []
    for f in top_factors or []:
        if isinstance(f, dict):
            out.append(str(f.get(key) or f.get("text_ru") or f.get("feature") or ""))
    return [t for t in out if t]


def prediction_view(r: Any, cfg: TwinConfig, lang: str = "ru") -> dict[str, Any]:
    eq = cfg.equipment.get(r["equipment"])
    return {
        "ts": r["ts"].isoformat(),
        "equipment": r["equipment"],
        "equipment_name_ru": eq.name_ru if eq else None,
        "type": eq.type if eq else None,
        "line": cfg.line_of_equipment(r["equipment"]).code if eq else None,
        "horizon_h": r["horizon_h"],
        "p_failure": r["p_failure"],
        "health_index": r["health_index"],
        "model_version": r["model_version"],
        "top_factors": r["top_factors"] or [],
        "factors": factor_texts(r["top_factors"], lang),
    }


def _encode(ts: str, equipment: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([ts, equipment]).encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, str]:
    try:
        ts, equipment = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(ts), str(equipment)
    except (ValueError, TypeError):
        raise ProblemError(
            422, "Request validation failed", "invalid cursor", slug="validation"
        ) from None


@router.get("/predictions", summary="PdM predictions (p_failure, health index, factors)")
async def list_predictions(
    principal: Maintainer,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    equipment: Annotated[list[str] | None, Query(description="unit code(s), repeat/comma")] = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    min_p: Annotated[float | None, Query(ge=0, le=1, description="p_failure >= min_p")] = None,
    latest: Annotated[bool, Query(description="only the newest prediction of each unit")] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 200,
    cursor: str | None = None,
) -> dict[str, Any]:
    codes = [c.strip() for item in (equipment or []) for c in item.split(",") if c.strip()]
    unknown = [c for c in codes if c not in cfg.equipment]
    if unknown:
        raise ProblemError(
            404, "Not Found", f"unknown equipment {', '.join(unknown)}", slug="not-found"
        )
    now = clock.now()
    hi = parse_instant(end, cfg, end=True) if end else now + timedelta(seconds=1)
    lo = parse_instant(start, cfg) if start else hi - DEFAULT_SPAN
    if hi <= lo:
        raise ProblemError(
            422, "Request validation failed", "'from' must be before 'to'", slug="validation"
        )
    where = ["ts >= :lo", "ts < :hi", "ts <= :now"]
    params: dict[str, Any] = {"lo": lo, "hi": hi, "now": now, "limit": limit + 1}
    if codes:
        where.append("equipment = ANY(:codes)")
        params["codes"] = codes
    if min_p is not None:
        where.append("p_failure >= :min_p")
        params["min_p"] = min_p
    if latest:
        sql = (
            "SELECT DISTINCT ON (equipment) equipment, ts, horizon_h, p_failure, health_index, "
            "model_version, top_factors FROM prediction "
            f"WHERE {' AND '.join(where)} ORDER BY equipment, ts DESC"
        )
        rows = (await session.execute(text(sql), params)).mappings().all()
        items = [prediction_view(r, cfg, principal_lang(principal)) for r in rows]
        items.sort(key=lambda i: (-i["p_failure"], i["equipment"]))
        return {"items": items, "next_cursor": None}
    if cursor:
        after_ts, after_eq = _decode(cursor)
        where.append("(ts, equipment) < (:after_ts, :after_eq)")
        params.update(after_ts=after_ts, after_eq=after_eq)
    sql = (
        "SELECT equipment, ts, horizon_h, p_failure, health_index, model_version, top_factors "
        f"FROM prediction WHERE {' AND '.join(where)} ORDER BY ts DESC, equipment DESC LIMIT :limit"
    )
    rows = (await session.execute(text(sql), params)).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [prediction_view(r, cfg, principal_lang(principal)) for r in rows]
    next_cursor = _encode(items[-1]["ts"], items[-1]["equipment"]) if more and items else None
    return {"items": items, "next_cursor": next_cursor}


def principal_lang(principal: Principal) -> str:
    return principal.lang if principal.lang in ("ru", "kk") else "ru"
