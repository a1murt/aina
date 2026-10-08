"""Authentication (JWT HS256) and role checks for every endpoint (SPEC §3, §12.1, NFR-04).

* ``POST /api/v1/auth/login`` verifies an argon2 password hash from ``app_user`` and issues a
  bearer token (``JWT_SECRET``, lifetime ``JWT_TTL_HOURS`` of *wall* time — security, not plant
  time). Claims: ``sub`` (user id), ``usr``, ``name``, ``role``, ``lang``, ``lines`` (lines an
  operator may act on), ``iat``, ``exp``, ``iss``.
* Every endpoint declares its roles with :func:`require_roles` (or :func:`require_any_role` /
  :func:`require_config_roles`); the principal comes from ``Authorization: Bearer <token>``
  (WebSocket: ``?token=``). Validation is stateless (signature + expiry), so role checks never
  touch the database and run before it (403 is answered even when the database is down).
* The allowed roles of a dependency are attached to it (:data:`ROLES_ATTR`) and exported to
  OpenAPI as ``x-roles`` (:mod:`qost_api.openapi`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qost_api.problems import ProblemError
from qost_api.settings import ApiSettings
from twin_core.clock import system_now
from twin_core.config import TwinConfig

ALGORITHM = "HS256"
ISSUER = "aina"
ROLES_ATTR = "qost_roles"
"""Attribute of a role dependency: a frozenset of roles, ``"*"`` (any role) or a callable
``TwinConfig -> Iterable[str]``."""
ANY_ROLE = "*"

_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False, description="JWT from POST /api/v1/auth/login")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    username: str
    role: str
    user_id: int | None = None
    """``app_user.id`` (``None`` only for internal callers such as ``make demo``'s import)."""
    display_name: str | None = None
    lang: str = "ru"
    lines: tuple[str, ...] = field(default=())
    """Lines an operator may act on (empty for other roles: they are not line-bound)."""

    def may_act_on_line(self, line: str) -> bool:
        """Operators act only on their own lines (§12.2 "operator (своя линия)")."""
        return self.role != "operator" or line in self.lines


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


_DUMMY_HASH = _hasher.hash("aina-timing-equaliser")


def burn_verify(password: str) -> None:
    """Spend one verification on an unknown user (no user-name oracle by response time)."""
    verify_password(_DUMMY_HASH, password)


# --------------------------------------------------------------------------- tokens


def signing_key(settings: ApiSettings) -> bytes:
    """HS256 key = SHA-256 of ``JWT_SECRET`` (always 32 bytes, RFC 7518 §3.2; a short secret is
    still weak — the app warns at start about the default one)."""
    return hashlib.sha256(settings.jwt_secret.get_secret_value().encode()).digest()


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    expires_at: datetime


def issue_token(
    settings: ApiSettings, principal: Principal, *, now: datetime | None = None
) -> IssuedToken:
    """Sign a token for ``principal`` (wall-clock ``iat``/``exp``)."""
    issued = now or system_now()
    expires = issued + timedelta(hours=settings.jwt_ttl_hours)
    claims: dict[str, Any] = {
        "sub": str(principal.user_id) if principal.user_id is not None else principal.username,
        "usr": principal.username,
        "name": principal.display_name or principal.username,
        "role": principal.role,
        "lang": principal.lang,
        "lines": list(principal.lines),
        "iss": ISSUER,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(claims, signing_key(settings), algorithm=ALGORITHM)
    return IssuedToken(token, expires)


def _unauthorized(detail: str) -> ProblemError:
    return ProblemError(
        401,
        "Unauthorized",
        detail,
        slug="unauthorized",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def decode_token(settings: ApiSettings, config: TwinConfig, token: str) -> Principal:
    """The principal of a token; 401 for a bad/expired token, 403 for an unknown role."""
    try:
        claims = jwt.decode(
            token,
            signing_key(settings),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "role", "usr"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("the token has expired, log in again") from None
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"invalid token: {exc}") from None
    role = str(claims["role"])
    if role not in config.rules.roles:
        raise ProblemError(403, "Forbidden", f"unknown role '{role}'", slug="forbidden")
    sub = str(claims["sub"])
    lines = claims.get("lines") or []
    return Principal(
        username=str(claims["usr"]),
        role=role,
        user_id=int(sub) if sub.isdigit() else None,
        display_name=str(claims.get("name") or claims["usr"]),
        lang=str(claims.get("lang") or "ru"),
        lines=tuple(str(x) for x in lines) if isinstance(lines, list) else (),
    )


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """The caller from ``Authorization: Bearer <JWT>`` (401 without valid credentials)."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized("credentials are required: Authorization: Bearer <token>")
    principal = decode_token(
        request.app.state.settings, request.app.state.config, credentials.credentials
    )
    request.state.principal = principal
    return principal


def _forbidden(principal: Principal, allowed: Iterable[str]) -> ProblemError:
    return ProblemError(
        403,
        "Forbidden",
        f"role '{principal.role}' may not do this (allowed: {', '.join(sorted(allowed))})",
        slug="forbidden",
    )


def require_roles(*roles: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency: the current principal, 403 unless its role is one of ``roles``."""
    allowed = frozenset(roles)

    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if principal.role not in allowed:
            raise _forbidden(principal, allowed)
        return principal

    setattr(dependency, ROLES_ATTR, allowed)
    return dependency


def require_any_role() -> Callable[..., Awaitable[Principal]]:
    """Dependency: any authenticated user with a configured role ("все" in §12.2)."""

    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        return principal

    setattr(dependency, ROLES_ATTR, ANY_ROLE)
    return dependency


def require_config_roles(
    select: Callable[[TwinConfig], Iterable[str]],
) -> Callable[..., Awaitable[Principal]]:
    """Dependency: roles computed from the plant config (e.g. alert recipients + admin)."""

    async def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        allowed = frozenset(select(request.app.state.config))
        if principal.role not in allowed:
            raise _forbidden(principal, allowed)
        return principal

    setattr(dependency, ROLES_ATTR, select)
    return dependency


def roles_of(dependency: object, config: TwinConfig | None) -> list[str] | None:
    """Allowed roles of a role dependency (``None`` if it is not one)."""
    spec = getattr(dependency, ROLES_ATTR, None)
    if spec is None:
        return None
    if spec == ANY_ROLE:
        return list(config.rules.roles) if config is not None else [ANY_ROLE]
    if callable(spec):
        return sorted(spec(config)) if config is not None else ["(per config)"]
    roles = set(spec)
    if config is not None:
        order = {r: i for i, r in enumerate(config.rules.roles)}
        return sorted(roles, key=lambda r: (order.get(r, len(order)), r))
    return sorted(roles)


AnyUser = Annotated[Principal, Depends(require_any_role())]
"""Endpoint parameter: any authenticated user."""
