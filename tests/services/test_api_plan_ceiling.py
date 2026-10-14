"""Line ceiling of a month (CONS-16, additive ``ceiling`` of ``GET /plan/progress``)."""

from __future__ import annotations

import pytest

from qost_api.plan_ceiling import line_ceiling
from twin_core.config import TwinConfig


def test_october_ceiling_is_the_slowest_line_over_the_calendar(cfg: TwinConfig) -> None:
    ceiling = line_ceiling(cfg, "2026-10")
    assert ceiling is not None
    slowest = max(cfg.flow_lines, key=lambda code: cfg.lines[code].ict_seconds)
    assert ceiling.line == slowest
    assert ceiling.ict_seconds == cfg.lines[slowest].ict_seconds
    assert ceiling.shifts == 42  # the October calendar (the plan progress test counts the same)
    assert ceiling.qty == 5191  # 42 x 480 min x 60 / 233 s = 5 191.4 (slide 2, README)
    assert ceiling.rate_per_shift == pytest.approx(480 * 60 / 233)
    assert ceiling.as_json() == {"qty": 5191, "rate_per_shift": 123.6, "shifts": 42}


def test_ceiling_follows_the_calendar(cfg: TwinConfig) -> None:
    october = line_ceiling(cfg, "2026-10")
    november = line_ceiling(cfg, "2026-11")
    assert october is not None
    assert november is not None
    assert november.qty == int(november.rate_per_shift * november.shifts + 1e-9)
    assert november.rate_per_shift == pytest.approx(october.rate_per_shift)
