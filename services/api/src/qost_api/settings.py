"""API settings (environment variables, SPEC §4.6)."""

from __future__ import annotations

from twin_core.settings import TwinSettings


class ApiSettings(TwinSettings):
    database_url: str | None = None
    """``DATABASE_URL`` (``postgresql+asyncpg://…``); without it data endpoints answer 503."""

    import_max_bytes: int = 10 * 1024 * 1024
    """``IMPORT_MAX_BYTES``: upper bound for one import upload (all files together)."""

    dev_auth_default_role: str | None = "admin"
    """``DEV_AUTH_DEFAULT_ROLE``: role assumed when a request has no ``X-Dev-Role`` header.

    DEV ONLY (until JWT in M4): see :mod:`qost_api.auth`. Empty -> such requests get 401.
    """
