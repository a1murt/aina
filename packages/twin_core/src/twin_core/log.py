"""Structured JSON logging (NFR-07)."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(service: str, *, level: str = "INFO") -> Any:
    """Configure structlog to emit one JSON object per line on stdout; return a bound logger."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        # No explicit file: structlog then resolves ``sys.stdout`` at write time, so a
        # stream swapped and closed later (tests, in-process CLI calls) is never cached.
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger().bind(service=service)
