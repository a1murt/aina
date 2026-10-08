"""Entry point: ``python -m qost_collector`` — the read-only collector (SPEC §7.1–7.3).

Subscribes to OPC UA nodes and MQTT topics of the tag map, normalizes values into
``twin_core.events`` and writes them in batches to the database and the Redis Stream ``events``,
with a disk spool when either is unavailable. Health on ``HEALTH_PORT`` (8110).
"""

from __future__ import annotations

import asyncio

SERVICE = "collector"
DEFAULT_PORT = 8110


def main() -> None:
    from qost_collector.service import run_service

    asyncio.run(run_service())


if __name__ == "__main__":
    main()
