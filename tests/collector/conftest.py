from __future__ import annotations

import pytest

from support import CONFIG_DIR
from twin_core.config import TwinConfig, load_config


@pytest.fixture(scope="session")
def cfg_with_map() -> TwinConfig:
    """The repository configuration with the generated demo tag map."""
    return load_config(CONFIG_DIR, tag_map=CONFIG_DIR / "tag_map.demo.yaml")
