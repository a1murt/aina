"""Role checks for every endpoint (SPEC §12.2, NFR-04).

!!! DEV IMPLEMENTATION — REPLACED IN M4 BY JWT (``Authorization: Bearer``) !!!

Until stage M4 the caller's identity is taken from headers:

* ``X-Dev-Role`` — role name (one of ``rules.yaml: roles``);
* ``X-Dev-User`` — user name for the audit log (default ``dev-<role>``).

Without ``X-Dev-Role`` the role ``DEV_AUTH_DEFAULT_ROLE`` (default ``admin``) is assumed; set it
empty to require the header. Endpoints declare their roles with :func:`require_roles` only, so
M4 swaps :func:`current_principal` for the JWT version without touching the routes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from qost_api.problems import ProblemError
from qost_api.settings import ApiSettings
from twin_core.config import TwinConfig


@dataclass(frozen=True, slots=True)
class Principal:
    username: str
    role: str
    user_id: int | None = None
    """``app_user.id``; ``None`` for the dev principal (no user rows before M4)."""


async def current_principal(
    request: Request,
    x_dev_role: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
) -> Principal:
    """DEV ONLY: identity from ``X-Dev-Role`` / ``X-Dev-User`` headers (M4: JWT)."""
    settings: ApiSettings = request.app.state.settings
    config: TwinConfig = request.app.state.config
    role = (x_dev_role or settings.dev_auth_default_role or "").strip()
    if not role:
        raise ProblemError(401, "Unauthorized", "no credentials", slug="unauthorized")
    if role not in config.rules.roles:
        raise ProblemError(403, "Forbidden", f"unknown role '{role}'", slug="forbidden")
    return Principal(username=(x_dev_user or f"dev-{role}").strip(), role=role)


def require_roles(*roles: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency: the current principal, 403 unless its role is one of ``roles``."""
    allowed = frozenset(roles)

    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if principal.role not in allowed:
            raise ProblemError(
                403,
                "Forbidden",
                f"role '{principal.role}' may not do this (allowed: {', '.join(sorted(allowed))})",
                slug="forbidden",
            )
        return principal

    return dependency
