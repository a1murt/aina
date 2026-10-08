"""``/api/v1/auth`` — login (JWT) and the caller's profile (SPEC §12.2: all roles)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from qost_api.auth import (
    AnyUser,
    burn_verify,
    hash_password,
    issue_token,
    needs_rehash,
    verify_password,
)
from qost_api.db import Session
from qost_api.deps import Settings
from qost_api.problems import ProblemError
from qost_api.users import find_user, operator_lines, principal_of, user_view

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = structlog.get_logger("qost_api.auth")


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserView(BaseModel):
    id: int | None
    username: str
    display_name: str | None
    role: str
    lang: str
    lines: list[str] = Field(description="lines an operator may act on (empty: not line-bound)")


class TokenView(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str = Field(description="ISO 8601 UTC (wall clock)")
    user: UserView


class _Throttle:
    """Failed logins per user name in a sliding window (in-process, wall monotonic time)."""

    def __init__(self) -> None:
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, username: str, limit: int, window_s: float) -> bool:
        q = self.failures[username]
        now = time.monotonic()
        while q and now - q[0] > window_s:
            q.popleft()
        return len(q) >= limit

    def fail(self, username: str) -> None:
        self.failures[username].append(time.monotonic())

    def clear(self, username: str) -> None:
        self.failures.pop(username, None)


def get_throttle(request: Request) -> _Throttle:
    throttle: _Throttle | None = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        throttle = request.app.state.login_throttle = _Throttle()
    return throttle


@router.post(
    "/login",
    summary="Log in: user name + password -> bearer token (JWT HS256)",
    responses={
        401: {"description": "Wrong user name or password"},
        429: {"description": "Too many failed attempts"},
    },
)
async def login(
    body: LoginBody,
    settings: Settings,
    session: Session,
    throttle: Annotated[_Throttle, Depends(get_throttle)],
) -> TokenView:
    username = body.username.strip()
    if throttle.blocked(username, settings.login_max_failures, settings.login_lock_s):
        raise ProblemError(
            429,
            "Too Many Requests",
            f"too many failed logins, retry in {settings.login_lock_s:g} s",
            slug="login-throttled",
            headers={"Retry-After": str(int(settings.login_lock_s))},
        )
    user = await find_user(session, username)
    if user is None:
        burn_verify(body.password)
    if user is None or not user.active or not verify_password(user.password_hash, body.password):
        throttle.fail(username)
        log.info("login_failed", username=username)
        raise ProblemError(
            401, "Unauthorized", "wrong user name or password", slug="invalid-credentials"
        )
    throttle.clear(username)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
        await session.commit()
    principal = principal_of(user, await operator_lines(session))
    issued = issue_token(settings, principal)
    log.info("login", username=username, role=principal.role)
    return TokenView(
        access_token=issued.token,
        expires_at=issued.expires_at.isoformat().replace("+00:00", "Z"),
        user=UserView(**user_view(principal)),
    )


@router.get("/me", summary="The caller's profile (from the token)")
async def me(principal: AnyUser) -> UserView:
    return UserView(**user_view(principal))
