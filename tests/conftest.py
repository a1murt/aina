"""Shared fixtures.

Layout: tests/<area>/test_*.py for unit tests (``make check``), tests/integration/ for tests that
need the running infrastructure (``@pytest.mark.integration``, ``make test``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from support import CONFIG_DIR, REPO_ROOT
from twin_core.config import TwinConfig, load_config


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture(scope="session")
def cfg() -> TwinConfig:
    """The repository configuration, loaded once (read-only)."""
    return load_config(CONFIG_DIR, tag_map=False)


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A writable copy of config/ for mutation tests."""
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    return target
