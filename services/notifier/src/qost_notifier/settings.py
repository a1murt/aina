"""Notifier settings (SPEC §4.6, §14). Environment variables only."""

from __future__ import annotations

from pydantic import SecretStr

from twin_core.settings import TwinSettings


def parse_role_pins(raw: str) -> dict[str, str]:
    """``director:1111,master:2222`` -> ``{"director": "1111", "master": "2222"}``."""
    pins: dict[str, str] = {}
    for part in raw.split(","):
        role, sep, pin = part.strip().partition(":")
        if sep and role.strip() and pin.strip():
            pins[role.strip()] = pin.strip()
    return pins


class NotifierSettings(TwinSettings):
    database_url: str = "postgresql+asyncpg://qost:qost@localhost:5432/qost"
    telegram_bot_token: SecretStr | None = None
    telegram_role_pins: str = ""
    """``TELEGRAM_ROLE_PINS``: ``role:pin`` pairs; only these roles can subscribe."""
    public_web_url: str = "http://localhost:3000"
    notifier_open_path: str = "/live?alert={id}"
    """Deep link of the «Открыть» button, appended to ``PUBLIC_WEB_URL``."""
    notifier_api_url: str = "http://localhost:8000"
    """The Qost API (internal network) used for «Принять» (``POST /api/v1/alerts/{id}/ack``)."""
    notifier_api_user: str = "admin"
    """Service account of the ack button (a seeded demo user, FR-DB-01)."""
    notifier_api_password: SecretStr | None = None
    """Default: ``DEMO_PASSWORD``."""
    demo_password: SecretStr = SecretStr("qost2026")
    alerts_stream: str = "alerts"
    notifier_group: str = "notifier"
    notifier_consumer: str = "notifier-1"
    notifier_dedup_window_s: float = 600.0
    """Same alert to the same chat at most once per window (wall time; SPEC §14: 10 min)."""
    notifier_alert_wait_s: float = 5.0
    """How long to wait for the engine to commit an alert row before sending it."""
    notifier_send_attempts: int = 5
    notifier_backoff_s: float = 1.0
    notifier_backoff_max_s: float = 30.0
    notifier_digest_check_s: float = 10.0
    health_port: int = 8130

    @property
    def pins(self) -> dict[str, str]:
        return parse_role_pins(self.telegram_role_pins)

    @property
    def token(self) -> str:
        return self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else ""

    @property
    def api_password(self) -> str:
        own = self.notifier_api_password.get_secret_value() if self.notifier_api_password else ""
        return own or self.demo_password.get_secret_value()


__all__ = ["NotifierSettings", "parse_role_pins"]
