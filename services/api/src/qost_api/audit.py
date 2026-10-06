"""Audit log of data changes (every data change is audited, NFR-04)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.auth import Principal
from twin_core.db import AuditLog


def audit(
    session: AsyncSession,
    *,
    ts: datetime,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: str | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an ``audit_log`` row to the session (committed with the change it describes).

    ``after`` carries the acting user name under ``"by"`` while users have no DB ids (M4).
    """
    payload = dict(after or {})
    payload.setdefault("by", principal.username)
    payload.setdefault("role", principal.role)
    row = AuditLog(
        ts=ts,
        user_id=principal.user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=payload,
    )
    session.add(row)
    return row
