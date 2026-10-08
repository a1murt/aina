"""Environment settings of the engine service (SPEC §4.6, §9)."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class EngineSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    database_url: str = "postgresql+asyncpg://qost:qost@localhost:5432/qost"
    redis_url: str = "redis://localhost:6379/0"
    events_stream: str = "events"
    engine_group: str = "engine"
    engine_consumer: str = "engine-1"
    live_prefix: str = "live:"
    live_channel: str = "live"
    alerts_stream: str = "alerts"
    alerts_stream_maxlen: int = 10_000
    sim_control_channel: str = "sim:control"
    collector_status_key: str = "collector:status"
    engine_checkpoint: str = "live"
    engine_commit_ms: int = 1000
    """Derived rows + checkpoint are committed at most this often (one transaction)."""
    engine_commit_events: int = 2000
    engine_publish_min_ms: int = 100
    """Live KPIs are published at most this often (wall time), and at least every kpi_tick_s."""
    engine_close_grace_wall_s: float = 2.0
    """A shift closes this long (wall time, x speed in plant time) after its end."""
    engine_read_count: int = 1000
    engine_block_ms: int = 100
    engine_max_pending_ops: int = 200_000
    """Write-behind cap while the database is down; beyond it the engine stops reading."""
    engine_line_state: Literal["events", "derive"] = "events"
    engine_replay_alerts: Literal["resolved", "open"] = "resolved"
    engine_reset_wait_s: float = 2.0
    health_port: int = 8120
