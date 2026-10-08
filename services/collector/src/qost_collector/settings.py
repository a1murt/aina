"""Environment settings of the collector (SPEC §4.6, §7.1–7.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    database_url: str = "postgresql+asyncpg://qost:qost@localhost:5432/qost"
    redis_url: str = "redis://localhost:6379/0"
    mqtt_url: str | None = None
    """``MQTT_URL``; default: the tag map's ``mqtt.url``."""
    opcua_endpoint: str | None = None
    """``OPCUA_ENDPOINT``; default: the tag map's ``opcua.endpoint``."""
    opcua_username: str | None = None
    opcua_password: str | None = None
    collector_opcua_cert: Path | None = None
    collector_opcua_key: Path | None = None
    collector_topic_root: str | None = None
    """Override of the tag map's ``mqtt.topic_root`` (tests)."""
    collector_signals: Literal["opcua", "mqtt"] = "opcua"
    """Source of states, alarms, telemetry, buffers and CKD stock."""
    collector_units: Literal["mqtt", "off"] = "mqtt"
    """Unit and defect events (body id, product, defect code) come only from the MQTT UNS
    ``…/{LINE}/units`` topics; OPC UA carries counters only (SPEC §6.7–6.8)."""
    collector_batch_max: int = 500
    collector_batch_ms: int = 200
    collector_opcua_queue_size: int = 32
    collector_join_grace_ms: int = 50
    collector_spool_dir: Path = Path("var/spool")
    collector_spool_segment_mb: float = 10.0
    collector_spool_max_gb: float = 2.0
    collector_output_queue: int = 100
    """Batches waiting per output before new ones go to the spool."""
    events_stream: str = "events"
    events_stream_maxlen: int = 300_000
    """~110 MB of Redis (365 B per entry measured); FR-ING-02 says ~1 M, which would take ~365 MB
    of the 512 MB Redis budget. The engine replays from ``event_raw`` if it falls further behind."""
    sim_control_channel: str = "sim:control"
    collector_status_key: str = "collector:status"
    health_port: int = 8110
