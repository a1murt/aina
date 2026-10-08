"""Client of the Qost API for the «Принять» button: ``POST /api/v1/alerts/{id}/ack``.

The notifier signs in as a service account (``NOTIFIER_API_USER``, default the seeded ``admin``
demo user, password ``NOTIFIER_API_PASSWORD`` or ``DEMO_PASSWORD``) with ``POST
/api/v1/auth/login`` and sends ``Authorization: Bearer``; a 401 triggers one fresh login. The
Telegram user's role is checked by the notifier before the call (recipients of the rule).
"""

from __future__ import annotations

from typing import Literal, Protocol

import aiohttp
import structlog

log = structlog.get_logger("qost_notifier.api")

AckResult = Literal["ok", "already", "forbidden", "not_found", "error"]


class AckClient(Protocol):
    async def ack(self, alert_id: int) -> AckResult: ...

    async def aclose(self) -> None: ...


class HttpAckClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_s: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session = session
        self._token: str | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _login(self) -> str | None:
        url = f"{self.base_url}/api/v1/auth/login"
        body = {"username": self.username, "password": self.password}
        async with self._http().post(url, json=body) as response:
            if response.status != 200:
                log.warning("api_login_failed", status=response.status, user=self.username)
                return None
            data = await response.json()
        token = data.get("access_token") or data.get("token")
        self._token = str(token) if token else None
        return self._token

    async def ack(self, alert_id: int) -> AckResult:
        url = f"{self.base_url}/api/v1/alerts/{alert_id}/ack"
        try:
            for attempt in (1, 2):
                token = self._token or await self._login()
                if token is None:
                    return "error"
                headers = {"Authorization": f"Bearer {token}"}
                async with self._http().post(url, headers=headers) as response:
                    status = response.status
                if status == 401 and attempt == 1:
                    self._token = None
                    continue
                return _result(status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("api_ack_failed", alert_id=alert_id, error=str(exc)[:200])
        return "error"


def _result(status: int) -> AckResult:
    if 200 <= status < 300:
        return "ok"
    if status == 409:
        return "already"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    return "error"


__all__ = ["AckClient", "AckResult", "HttpAckClient"]
