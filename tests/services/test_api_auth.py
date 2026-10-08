"""JWT login and token checks (SPEC §12.1, NFR-04): argon2 passwords, claims, expiry, 401/403."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from api_support import PROBLEM, FakeSession, bearer, token
from qost_api import auth as auth_module
from qost_api.app import create_app
from qost_api.auth import hash_password, signing_key, verify_password
from qost_api.db import get_session
from qost_api.routes import auth as auth_routes
from qost_api.settings import ApiSettings
from twin_core.clock import system_now
from twin_core.config import TwinConfig
from twin_core.db import AppUser

PASSWORD = "s3cret-demo"
_HASH = hash_password(PASSWORD)


def _user(username: str, role: str, *, active: bool = True) -> AppUser:
    return AppUser(
        id=7,
        username=username,
        display_name=f"Demo {role}",
        role=role,
        lang="ru",
        password_hash=_HASH,
        active=active,
    )


@pytest.fixture
def client(cfg: TwinConfig, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    users = {
        "operator": _user("operator", "operator"),
        "director": _user("director", "director"),
        "retired": _user("retired", "master", active=False),
    }

    async def find_user(session: object, username: str) -> AppUser | None:
        return users.get(username)

    async def operator_lines(session: object) -> dict[str, list[str]]:
        return {"operator": ["ASSY-1"]}

    monkeypatch.setattr(auth_routes, "find_user", find_user)
    monkeypatch.setattr(auth_routes, "operator_lines", operator_lines)
    app = create_app(cfg, database_url=None, redis=None)

    async def fake_session() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    app.dependency_overrides[get_session] = fake_session
    with TestClient(app) as test_client:
        yield test_client


def test_password_hashing_is_argon2() -> None:
    assert _HASH.startswith("$argon2id$")
    assert verify_password(_HASH, PASSWORD)
    assert not verify_password(_HASH, "wrong")
    assert not verify_password("not-a-hash", PASSWORD)


def test_login_issues_a_token_with_role_lang_and_lines(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": "operator", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"] == {
        "id": 7,
        "username": "operator",
        "display_name": "Demo operator",
        "role": "operator",
        "lang": "ru",
        "lines": ["ASSY-1"],
    }
    claims = jwt.decode(
        body["access_token"], signing_key(ApiSettings()), algorithms=["HS256"], issuer="aina"
    )
    assert claims["role"] == "operator"
    assert claims["lines"] == ["ASSY-1"]
    assert claims["sub"] == "7"
    assert claims["exp"] - claims["iat"] == 12 * 3600  # JWT_TTL_HOURS (wall clock)
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["lines"] == ["ASSY-1"]


def test_non_operators_are_not_line_bound(client: TestClient) -> None:
    body = client.post(
        "/api/v1/auth/login", json={"username": "director", "password": PASSWORD}
    ).json()
    assert body["user"]["lines"] == []


@pytest.mark.parametrize(
    ("username", "password"),
    [("operator", "wrong"), ("nobody", PASSWORD), ("retired", PASSWORD)],
)
def test_bad_credentials_are_401(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["type"] == "/problems/invalid-credentials"
    assert body["detail"] == "wrong user name or password"  # same text: no user-name oracle


def test_repeated_failures_are_throttled(client: TestClient) -> None:
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "director", "password": "x"})
    response = client.post(
        "/api/v1/auth/login", json={"username": "director", "password": PASSWORD}
    )
    assert response.status_code == 429
    assert response.json()["type"] == "/problems/login-throttled"
    assert "retry-after" in response.headers


def test_login_body_is_validated(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "operator"})
    assert response.status_code == 422
    assert response.json()["type"] == "/problems/validation"


def test_expired_token_is_401(client: TestClient) -> None:
    old = token("director", now=system_now() - timedelta(hours=13))
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_foreign_signature_is_401(client: TestClient) -> None:
    forged = jwt.encode(
        {"sub": "1", "usr": "x", "role": "admin", "iat": 1, "exp": 2**31, "iss": "aina"},
        b"another-secret-of-thirty-two-bytes!!",
        algorithm="HS256",
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert response.json()["type"] == "/problems/unauthorized"


def test_unknown_role_in_token_is_403(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me", headers=bearer("root"))
    assert response.status_code == 403
    assert "unknown role" in response.json()["detail"]


def test_principal_line_rule() -> None:
    operator = auth_module.Principal("op", "operator", lines=("ASSY-1",))
    assert operator.may_act_on_line("ASSY-1")
    assert not operator.may_act_on_line("WELD-1")
    assert auth_module.Principal("m", "master").may_act_on_line("WELD-1")
