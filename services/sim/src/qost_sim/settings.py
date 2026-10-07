"""Environment settings of the simulator service (SPEC §4.6)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SimSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    redis_url: str = "redis://localhost:6379/0"
    mqtt_url: str = "mqtt://localhost:1883"
    opcua_endpoint: str = "opc.tcp://sim:4840/qost/"
    """``OPCUA_ENDPOINT``: how clients reach the server (written into the demo tag map)."""

    sim_opcua_bind: str = "opc.tcp://0.0.0.0:4840/qost/"
    """``SIM_OPCUA_BIND``: where the server listens."""
    sim_http_host: str = "0.0.0.0"
    sim_http_port: int = 8100
    sim_autostart: bool = True
    """``SIM_AUTOSTART``: start the plant clock right after warm-up (else paused at demo_start)."""
    sim_clock_key: str = "plant:clock"
    sim_state_key: str = "sim:state"
    sim_control_channel: str = "sim:control"
    sim_reset_ack_timeout_s: float = 30.0
    sim_reset_flush_s: float = 1.0
    """Pause before the reset handshake so the collector can flush its last batch."""
    sim_tick_s: float = 0.1
    sim_mqtt: bool = True
    sim_resume: bool = True
    """``SIM_RESUME``: after a restart, replay to the last published plant time (same epoch)."""
