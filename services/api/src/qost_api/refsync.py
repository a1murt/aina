"""Reference data from YAML into SQL (SPEC §5.1, §8, FR-DB-01).

* :func:`sync_reference` mirrors ``plant.yaml`` / ``reason_codes.yaml`` / ``defect_codes.yaml``
  into ``asset_area``, ``asset_line``, ``asset_equipment``, ``asset_buffer``, ``product``,
  ``reason_code``, ``defect_code`` (upsert, then delete codes no longer in the config); runs at
  api startup and in ``make seed``.
* :func:`materialize_shifts` writes the ``shift`` calendar (every calendar shift slot with its
  ``working`` flag) for ±``days`` around a date.
* :func:`load_plan` copies ``plant.yaml: plan`` into ``production_plan`` without overwriting
  rows that exist (an imported plan wins).

All three run in the caller's transaction, serialised by a transaction-level advisory lock so
that a seed and an api start at the same moment do not deadlock.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from twin_core.config import TwinConfig
from twin_core.db import (
    AssetArea,
    AssetBuffer,
    AssetEquipment,
    AssetLine,
    DefectCode,
    Product,
    ProductionPlan,
    ReasonCode,
    Shift,
)

_LOCK_KEY = 4_200_240_001
"""``pg_advisory_xact_lock`` key of reference synchronisation."""


async def _lock(conn: AsyncConnection | AsyncSession) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})


async def _upsert(
    conn: AsyncConnection | AsyncSession, model: Any, rows: list[dict[str, Any]], key: str = "code"
) -> None:
    if not rows:
        return
    stmt = insert(model).values(rows)
    columns = {c: stmt.excluded[c] for c in rows[0] if c != key}
    await conn.execute(stmt.on_conflict_do_update(index_elements=[key], set_=columns))


async def _prune(conn: AsyncConnection | AsyncSession, model: Any, keep: list[str]) -> int:
    result = await conn.execute(delete(model).where(model.code.not_in(keep)))
    return int(getattr(result, "rowcount", 0) or 0)


def reference_rows(cfg: TwinConfig) -> dict[str, list[dict[str, Any]]]:
    """Rows of every reference table (pure; used by the sync and its tests)."""
    plant = cfg.plant
    areas = [
        {"code": a.code, "kind": a.kind, "name_ru": a.name_ru, "name_kk": a.name_kk, "seq": i}
        for i, a in enumerate(plant.areas, start=1)
    ]
    lines: list[dict[str, Any]] = []
    equipment: list[dict[str, Any]] = []
    for area in plant.areas:
        for line in area.lines:
            lines.append(
                {
                    "code": line.code,
                    "area": area.code,
                    "name_ru": line.name_ru,
                    "ict_seconds": line.ict_seconds,
                    "plan_rate_per_shift": line.plan_rate_per_shift,
                }
            )
            equipment.extend(
                {
                    "code": eq.code,
                    "line": line.code,
                    "type": eq.type,
                    "criticality": eq.criticality,
                    "degraded_capacity": eq.degraded_capacity,
                    "name_ru": eq.name_ru,
                }
                for eq in line.equipment
            )
    buffers = [
        {"code": b.code, "from_line": b.from_line, "to_line": b.to_line, "capacity": b.capacity}
        for b in plant.buffers
    ]
    products = [
        {"code": p.code, "name": p.name, "cycle_factor": p.cycle_factor} for p in plant.products
    ]
    reasons = [
        {
            "code": r.code,
            "category": c.code,
            "name_ru": r.name_ru,
            "name_kk": r.name_kk,
            "planned": r.planned,
            "bucket": r.bucket,
        }
        for c in cfg.reason_codes.categories
        for r in c.reasons
    ]
    defects = [
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
    return {
        "asset_area": areas,
        "asset_line": lines,
        "asset_equipment": equipment,
        "asset_buffer": buffers,
        "product": products,
        "reason_code": reasons,
        "defect_code": defects,
    }


async def sync_reference(conn: AsyncConnection | AsyncSession, cfg: TwinConfig) -> dict[str, int]:
    """Mirror the YAML reference data (see the module docstring); returns row counts."""
    await _lock(conn)
    rows = reference_rows(cfg)
    # children first when pruning (foreign keys), parents first when upserting
    pruned = 0
    pruned += await _prune(conn, AssetBuffer, [r["code"] for r in rows["asset_buffer"]])
    pruned += await _prune(conn, AssetEquipment, [r["code"] for r in rows["asset_equipment"]])
    pruned += await _prune(conn, AssetLine, [r["code"] for r in rows["asset_line"]])
    pruned += await _prune(conn, AssetArea, [r["code"] for r in rows["asset_area"]])
    for model, name in (
        (AssetArea, "asset_area"),
        (AssetLine, "asset_line"),
        (AssetEquipment, "asset_equipment"),
        (AssetBuffer, "asset_buffer"),
        (Product, "product"),
        (ReasonCode, "reason_code"),
        (DefectCode, "defect_code"),
    ):
        await _upsert(conn, model, rows[name])
    for model, name in (
        (Product, "product"),
        (ReasonCode, "reason_code"),
        (DefectCode, "defect_code"),
    ):
        pruned += await _prune(conn, model, [r["code"] for r in rows[name]])
    counts = {name: len(items) for name, items in rows.items()}
    counts["pruned"] = pruned
    return counts


async def materialize_shifts(
    conn: AsyncConnection | AsyncSession, cfg: TwinConfig, around: date, *, days: int = 120
) -> int:
    """Upsert every calendar shift slot of ``around ± days`` (FR-DB-01, §5.2)."""
    await _lock(conn)
    rows = [
        {
            "shift_date": s.shift_date,
            "shift_code": s.code,
            "start_ts": s.start,
            "end_ts": s.end,
            "working": s.working,
        }
        for s in cfg.calendar.materialize(
            around - timedelta(days=days), around + timedelta(days=days)
        )
    ]
    for i in range(0, len(rows), 500):
        chunk = rows[i : i + 500]
        stmt = insert(Shift).values(chunk)
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["shift_date", "shift_code"],
                set_={
                    "start_ts": stmt.excluded.start_ts,
                    "end_ts": stmt.excluded.end_ts,
                    "working": stmt.excluded.working,
                },
            )
        )
    return len(rows)


async def load_plan(conn: AsyncConnection | AsyncSession, cfg: TwinConfig) -> int:
    """Insert ``plant.yaml: plan`` rows missing from ``production_plan``; returns rows added."""
    added = 0
    for entry in cfg.plant.plan:
        stmt = (
            insert(ProductionPlan)
            .values(
                month=entry.month,
                level=entry.level,
                line=entry.line,
                product=entry.product,
                qty=entry.qty,
            )
            .on_conflict_do_nothing(constraint="uq_production_plan_month_level_line_product")
            .returning(ProductionPlan.id)
        )
        result = await conn.execute(stmt)
        added += len(result.fetchall())
    return added


async def shift_count(conn: AsyncConnection | AsyncSession) -> int:
    return int((await conn.execute(select(func.count()).select_from(Shift))).scalar_one())
