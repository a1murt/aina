"""API settings (environment variables, SPEC §4.6)."""

from __future__ import annotations

from pydantic import Field, SecretStr

from twin_core.settings import TwinSettings

DEFAULT_JWT_SECRET = "change-me"


class ApiSettings(TwinSettings):
    database_url: str | None = None
    """``DATABASE_URL`` (``postgresql+asyncpg://…``); without it data endpoints answer 503."""

    import_max_bytes: int = 10 * 1024 * 1024
    """``IMPORT_MAX_BYTES``: upper bound for one import upload (all files together)."""

    # ------------------------------------------------------------------ auth (NFR-04)
    jwt_secret: SecretStr = SecretStr(DEFAULT_JWT_SECRET)
    """``JWT_SECRET``: HS256 signing key (the default is refused outside development: a warning
    is logged at start)."""
    jwt_ttl_hours: float = Field(default=12.0, gt=0)
    """``JWT_TTL_HOURS``: token lifetime, wall-clock hours (not plant time)."""
    demo_password: SecretStr = SecretStr("qost2026")
    """``DEMO_PASSWORD``: password of the demo users created by ``make seed``."""
    demo_operator_lines: str | None = None
    """``DEMO_OPERATOR_LINES``: comma-separated lines of the seeded ``operator`` (default: the
    line that carries the model plan in ``plant.yaml``)."""
    login_max_failures: int = 5
    """Failed logins per user name within ``login_lock_s`` before answering 429."""
    login_lock_s: float = 60.0

    cors_origins: str = "http://localhost:3000"
    """``CORS_ORIGINS``: comma-separated allow-list (NFR-04); empty = no CORS headers."""

    # ------------------------------------------------------------------ live (Redis, §12.3)
    redis_enabled: bool = True
    """``REDIS_ENABLED=false`` runs without Redis (live endpoints answer 503)."""
    live_prefix: str = "live:"
    live_channel: str = "live"
    events_stream: str = "events"
    events_stream_maxlen: int = 300_000
    alerts_stream: str = "alerts"
    alerts_stream_maxlen: int = 10_000
    ws_min_interval_ms: int = 500
    """WebSocket throttle: at most one message per type/entity per this interval (2/s)."""
    ws_clock_tick_s: float = 5.0
    """``clock`` messages with the plant time at least this often (wall seconds)."""
    ws_queue_size: int = 2000
    """Per-client backlog; on overflow the client is re-synchronised with a snapshot."""

    # ------------------------------------------------------------------ demo console (§6.9)
    sim_control_url: str | None = None
    """``SIM_CONTROL_URL`` (e.g. ``http://sim:8100``): set only in the ``demo`` profile; without
    it ``/api/v1/sim/*`` answers 503."""
    sim_control_timeout_s: float = 35.0
    """Proxy timeout (the reset handshake may wait up to 30 s for the engine)."""
