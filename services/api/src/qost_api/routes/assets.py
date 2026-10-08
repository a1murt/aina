"""Asset tree with the plant schema layout and the configuration (SPEC §5.1, §12.2, §13.2).

``GET /assets``, ``GET /config/reasons|defects|rules`` — all roles; ``PATCH /config/thresholds`` —
admin (stored in ``settings``, audited; effective thresholds = ``rules.yaml`` overlaid by
``settings``, :mod:`twin_core.thresholds`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qost_api.audit import audit
from qost_api.auth import AnyUser, Principal, require_roles
from qost_api.db import Session
from qost_api.deps import Config, PlantClock
from qost_api.problems import ProblemError
from qost_api.queries.settings import stored_overrides
from twin_core.config import Signal, TwinConfig
from twin_core.db import Setting
from twin_core.thresholds import (
    OverrideError,
    apply_rule_overrides,
    override_fields,
)

router = APIRouter(prefix="/api/v1", tags=["assets", "config"])
Admin = Annotated[Principal, Depends(require_roles("admin"))]


# --------------------------------------------------------------------------- assets


def assets_view(cfg: TwinConfig) -> dict[str, Any]:
    """Everything the SVG plant schema and the UI need (codes, names, layout, limits)."""
    plant = cfg.plant
    site = plant.site

    def signal(s: Signal) -> dict[str, Any]:
        return dict(s.model_dump(mode="json"))

    areas: list[dict[str, Any]] = []
    for seq, area in enumerate(plant.areas, start=1):
        lines = []
        for line in area.lines:
            lines.append(
                {
                    "code": line.code,
                    "name_ru": line.name_ru,
                    "name_kk": line.name_kk,
                    "ict_seconds": line.ict_seconds,
                    "plan_rate_per_shift": line.plan_rate_per_shift,
                    "rework": line.rework.model_dump(mode="json"),
                    "equipment": [
                        {
                            "code": eq.code,
                            "name_ru": eq.name_ru,
                            "name_kk": eq.name_kk,
                            "type": eq.type,
                            "criticality": eq.criticality,
                            "degraded_capacity": eq.degraded_capacity,
                            "line": line.code,
                            "area": area.code,
                            "layout": eq.layout.model_dump(mode="json"),
                        }
                        for eq in line.equipment
                    ],
                }
            )
        areas.append(
            {
                "code": area.code,
                "kind": area.kind,
                "name_ru": area.name_ru,
                "name_kk": area.name_kk,
                "seq": seq,
                "layout": area.layout.model_dump(mode="json"),
                "lines": lines,
            }
        )
    return {
        "site": {
            "code": site.code,
            "name_ru": site.name_ru,
            "name_kk": site.name_kk,
            "timezone": site.timezone,
            "currency": site.currency,
        },
        "calendar": {
            "shifts": [
                {
                    "code": s.code,
                    "name_ru": s.name_ru,
                    "name_kk": s.name_kk,
                    "start": s.start.strftime("%H:%M"),
                    "end": s.end.strftime("%H:%M"),
                }
                for s in plant.calendar.shifts
            ],
            "working_weekdays": list(plant.calendar.working_weekdays),
            "holidays": [h.model_dump(mode="json") for h in plant.calendar.holidays],
            "extra_working_days": [
                d.model_dump(mode="json") for d in plant.calendar.extra_working_days
            ],
        },
        "flow": list(cfg.flow_lines),
        "areas": areas,
        "buffers": [
            {
                "code": b.code,
                "name_ru": b.name_ru,
                "name_kk": b.name_kk,
                "from_line": b.from_line,
                "to_line": b.to_line,
                "capacity": b.capacity,
                "layout": b.layout.model_dump(mode="json"),
            }
            for b in plant.buffers
        ],
        "products": [
            {
                "code": p.code,
                "name": p.name,
                "cycle_factor": p.cycle_factor,
                "color_hex": p.color_hex,
            }
            for p in plant.products
        ],
        "equipment_types": {
            code: {
                "name_ru": t.name_ru,
                "name_kk": t.name_kk,
                "signals": [signal(s) for s in t.signals],
            }
            for code, t in plant.equipment_types.items()
        },
        "layout": {
            "viewbox": list(plant.layout.viewbox),
            "flow_path": [list(p) for p in plant.layout.flow_path],
        },
        "plan": [e.model_dump(mode="json") for e in plant.plan],
    }


@router.get("/assets", summary="Asset tree (ISA-95) and plant schema layout")
async def get_assets(principal: AnyUser, cfg: Config) -> dict[str, Any]:
    return assets_view(cfg)


# --------------------------------------------------------------------------- reference data


@router.get("/config/reasons", summary="Downtime reason tree (reason_codes.yaml)")
async def get_reasons(principal: AnyUser, cfg: Config) -> dict[str, Any]:
    return {
        "categories": [
            {
                "code": c.code,
                "name_ru": c.name_ru,
                "name_kk": c.name_kk,
                "reasons": [
                    {
                        "code": r.code,
                        "name_ru": r.name_ru,
                        "name_kk": r.name_kk,
                        "planned": r.planned,
                        "bucket": r.bucket,
                    }
                    for r in c.reasons
                ],
            }
            for c in cfg.reason_codes.categories
        ]
    }


@router.get("/config/defects", summary="Defect codes (defect_codes.yaml)")
async def get_defects_config(principal: AnyUser, cfg: Config) -> dict[str, Any]:
    return {
        "defects": [
            {
                "code": d.code,
                "area": d.area,
                "name_ru": d.name_ru,
                "name_kk": d.name_kk,
                "disposition": d.disposition,
                "rework_min": d.rework_min,
                "repaint": d.repaint,
            }
            for d in cfg.defect_codes.defects
        ]
    }


def rules_view(
    cfg: TwinConfig, overrides: dict[str, dict[str, Any]], meta: dict[str, Any]
) -> dict[str, Any]:
    rules = apply_rule_overrides(cfg.rules, overrides)
    return {
        "thresholds": rules.thresholds.model_dump(mode="json"),
        "data_quality": rules.data_quality.model_dump(mode="json"),
        "defaults": {
            "thresholds": cfg.rules.thresholds.model_dump(mode="json"),
            "data_quality": cfg.rules.data_quality.model_dump(mode="json"),
        },
        "overrides": overrides,
        "overrides_meta": meta,
        "roles": list(rules.roles),
        "alert_rules": [r.model_dump(mode="json") for r in rules.alert_rules],
        "escalation": {k: v.model_dump(mode="json") for k, v in rules.escalation.items()},
        "data_quality_rules": [r.model_dump(mode="json") for r in rules.data_quality_rules],
        "engine": rules.engine.model_dump(mode="json"),
    }


@router.get("/config/rules", summary="Effective thresholds, alert and DQ rules")
async def get_rules(principal: AnyUser, cfg: Config, request: Request) -> dict[str, Any]:
    sessions: async_sessionmaker[AsyncSession] | None = request.app.state.sessionmaker
    if sessions is None:
        return rules_view(cfg, {}, {})
    async with sessions() as session:
        overrides, meta = await stored_overrides(session)
    return rules_view(cfg, overrides, meta)


class ThresholdsPatch(BaseModel):
    """Partial overrides; a ``null`` value removes the override (back to ``rules.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    thresholds: dict[str, Any] | None = None
    data_quality: dict[str, Any] | None = None


@router.patch(
    "/config/thresholds",
    summary="Change thresholds (settings + audit)",
    responses={422: {"description": "Unknown threshold, wrong value or broken ordering"}},
)
async def patch_thresholds(
    body: ThresholdsPatch, principal: Admin, session: Session, cfg: Config, clock: PlantClock
) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not changes:
        raise ProblemError(422, "Request validation failed", "nothing to change", slug="validation")
    current, _ = await stored_overrides(session)
    proposed = {key: dict(values) for key, values in current.items()}
    for key, values in changes.items():
        section = proposed.setdefault(key, {})
        for name, value in values.items():
            if value is None:
                section.pop(name, None)
            else:
                section[name] = value
    allowed = override_fields(cfg.rules)
    unknown = [
        {"loc": ["body", key, name], "msg": "unknown threshold", "type": "extra_forbidden"}
        for key, values in changes.items()
        for name in values
        if name not in allowed[key]
    ]
    if unknown:
        raise ProblemError(422, "Request validation failed", slug="validation", errors=unknown)
    try:
        apply_rule_overrides(cfg.rules, proposed)
    except OverrideError as exc:
        errors = [{**e, "loc": ["body", *e["loc"]]} for e in exc.errors]
        raise ProblemError(
            422, "Request validation failed", str(exc), slug="validation", errors=errors
        ) from None
    now = clock.now()
    for key in changes:
        value = proposed.get(key) or {}
        stmt = insert(Setting).values(
            key=key, value=value, updated_by=principal.user_id, updated_ts=now
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": stmt.excluded.value,
                    "updated_by": stmt.excluded.updated_by,
                    "updated_ts": stmt.excluded.updated_ts,
                },
            )
        )
        audit(
            session,
            ts=now,
            principal=principal,
            action="config.thresholds",
            entity_type="settings",
            entity_id=key,
            before={"overrides": current.get(key, {})},
            after={"overrides": value},
        )
    await session.commit()
    overrides, meta = await stored_overrides(session)
    return rules_view(cfg, overrides, meta)
