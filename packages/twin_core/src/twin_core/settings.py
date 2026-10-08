"""Process settings shared by all services (SPEC §4.6). Read from environment variables only."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_MARKER = Path("config") / "plant.yaml"


def default_config_dir() -> Path:
    """Locate the repository ``config/`` directory.

    Search upwards from this file (editable/workspace installs), then from the working directory;
    fall back to ``./config``. Containers set ``PLANT_CONFIG_DIR`` explicitly.
    """
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for directory in (start, *start.parents):
            if (directory / _MARKER).is_file():
                return directory / "config"
    return Path("config")


class TwinSettings(BaseSettings):
    """Settings common to every Aina process."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    plant_config_dir: Path = Field(default_factory=default_config_dir)
    """``PLANT_CONFIG_DIR``: directory with plant.yaml, rules.yaml, ..."""

    plant_tag_map: str | None = None
    """``PLANT_TAG_MAP``: tag map file (name in the config dir or a path). Default: the first
    existing of ``tag_map.yaml`` (pilot) and ``tag_map.demo.yaml`` (generated for the demo)."""

    clock_mode: Literal["sim", "system"] = "system"
    """``CLOCK_MODE``: ``sim`` — plant time from Redis ``plant:clock``; ``system`` — wall clock."""

    redis_url: str = "redis://localhost:6379/0"
    offline: bool = True
    """``OFFLINE``: forbid external network calls (LLM APIs, Telegram, CDNs)."""
