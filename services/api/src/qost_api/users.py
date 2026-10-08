"""Users (``app_user``) and their line binding.

The line an operator works on is kept in ``settings`` under :data:`OPERATOR_LINES_KEY` as
``{"<username>": ["ASSY-1", ...]}`` (no schema change: ``app_user`` of SPEC §8 has no line
column). It is copied into the token at login (claim ``lines``); masters and other roles are not
line-bound.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.auth import Principal
from twin_core.db import AppUser, Setting

OPERATOR_LINES_KEY = "operator_lines"


async def find_user(session: AsyncSession, username: str) -> AppUser | None:
    user: AppUser | None = await session.scalar(select(AppUser).where(AppUser.username == username))
    return user


async def operator_lines(session: AsyncSession) -> dict[str, list[str]]:
    row = await session.get(Setting, OPERATOR_LINES_KEY)
    value: Any = row.value if row is not None else None
    if not isinstance(value, Mapping):
        return {}
    return {str(k): [str(x) for x in v] for k, v in value.items() if isinstance(v, list)}


def principal_of(user: AppUser, lines: Mapping[str, list[str]]) -> Principal:
    return Principal(
        username=user.username,
        role=user.role,
        user_id=user.id,
        display_name=user.display_name,
        lang=user.lang,
        lines=tuple(lines.get(user.username, ())) if user.role == "operator" else (),
    )


def user_view(principal: Principal) -> dict[str, Any]:
    return {
        "id": principal.user_id,
        "username": principal.username,
        "display_name": principal.display_name,
        "role": principal.role,
        "lang": principal.lang,
        "lines": list(principal.lines),
    }
