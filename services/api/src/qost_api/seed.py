"""``make seed`` (FR-DB-01): demo users of every role, reference tables, shift calendar, plan.

Idempotent: users are upserted by name (the password hash is replaced only when it no longer
matches ``DEMO_PASSWORD``), reference tables are mirrored from YAML, ``shift`` is upserted and the
plan only fills months/rows that are missing. Every run leaves one ``audit_log`` entry
``seed.run`` with the counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qost_api.auth import hash_password, needs_rehash, verify_password
from qost_api.refsync import load_plan, materialize_shifts, sync_reference
from qost_api.settings import ApiSettings
from qost_api.users import OPERATOR_LINES_KEY
from twin_core.clock import Clock
from twin_core.config import TwinConfig
from twin_core.db import AppUser, AuditLog, Setting

DISPLAY_NAMES = {
    "director": "Директор (демо)",
    "master": "Мастер смены (демо)",
    "operator": "Оператор (демо)",
    "maintenance": "Механик ТОиР (демо)",
    "quality": "Контролёр ОТК (демо)",
    "admin": "Администратор (демо)",
}
"""Demo user names shown in the UI header (demo data, not plant configuration)."""


@dataclass
class SeedReport:
    users_created: list[str] = field(default_factory=list)
    users_updated: list[str] = field(default_factory=list)
    reference: dict[str, int] = field(default_factory=dict)
    shifts: int = 0
    plan_rows_added: int = 0
    operator_lines: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "users_created": self.users_created,
            "users_updated": self.users_updated,
            "reference": self.reference,
            "shifts": self.shifts,
            "plan_rows_added": self.plan_rows_added,
            "operator_lines": self.operator_lines,
        }


def default_operator_lines(cfg: TwinConfig, settings: ApiSettings) -> list[str]:
    """``DEMO_OPERATOR_LINES``, else the line(s) carrying the model plan in ``plant.yaml``."""
    if settings.demo_operator_lines:
        lines = [x.strip() for x in settings.demo_operator_lines.split(",") if x.strip()]
    else:
        lines = sorted({e.line for e in cfg.plant.plan if e.line is not None})
    unknown = [x for x in lines if x not in cfg.lines]
    if unknown:
        raise ValueError(f"unknown operator line(s) {unknown} (known: {', '.join(cfg.lines)})")
    return lines or [cfg.flow_lines[0]]


def seed_around(cfg: TwinConfig, settings: ApiSettings, clock: Clock | None) -> date:
    """Centre of the materialised calendar: demo start in sim mode, else today (plant zone)."""
    if settings.clock_mode == "sim" or clock is None:
        return cfg.simulation.clock.demo_start.astimezone(cfg.timezone).date()
    return clock.now().astimezone(cfg.timezone).date()


async def _users(session: AsyncSession, cfg: TwinConfig, password: str, report: SeedReport) -> None:
    existing = {
        u.username: u
        for u in (
            await session.scalars(select(AppUser).where(AppUser.username.in_(cfg.rules.roles)))
        ).all()
    }
    for role in cfg.rules.roles:
        user = existing.get(role)
        name = DISPLAY_NAMES.get(role, role)
        if user is None:
            await session.execute(
                insert(AppUser)
                .values(
                    username=role,
                    display_name=name,
                    role=role,
                    lang="ru",
                    password_hash=hash_password(password),
                    active=True,
                )
                .on_conflict_do_nothing(index_elements=["username"])
            )
            report.users_created.append(role)
            continue
        changed = False
        if user.role != role or not user.active or user.display_name != name:
            user.role, user.active, user.display_name = role, True, name
            changed = True
        if not verify_password(user.password_hash, password) or needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            changed = True
        if changed:
            report.users_updated.append(role)


async def run_seed(
    sessions: async_sessionmaker[AsyncSession],
    cfg: TwinConfig,
    settings: ApiSettings,
    *,
    around: date,
    now: Any,
) -> SeedReport:
    """One transaction: reference data, calendar, plan, users, operator lines, audit."""
    report = SeedReport()
    async with sessions() as session, session.begin():
        report.reference = await sync_reference(session, cfg)
        report.shifts = await materialize_shifts(session, cfg, around)
        report.plan_rows_added = await load_plan(session, cfg)
        await _users(session, cfg, settings.demo_password.get_secret_value(), report)
        lines = (
            {"operator": default_operator_lines(cfg, settings)}
            if "operator" in cfg.rules.roles
            else {}
        )
        row = await session.get(Setting, OPERATOR_LINES_KEY)
        merged = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        merged.update({k: v for k, v in lines.items() if k not in merged})
        report.operator_lines = merged
        stmt = insert(Setting).values(key=OPERATOR_LINES_KEY, value=merged, updated_ts=now)
        await session.execute(
            stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value})
        )
        session.add(
            AuditLog(
                ts=now,
                user_id=None,
                action="seed.run",
                entity_type="seed",
                entity_id=None,
                before=None,
                after={"by": "make seed", **report.as_dict()},
            )
        )
    return report
