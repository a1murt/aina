"""Entry point: ``python -m qost_api [serve|seed|import FILE...]``.

* ``serve`` (default): uvicorn on :8000.
* ``seed``: ``make seed`` (FR-DB-01) — users of every role, reference tables, calendar, plan.
* ``import FILE...``: import case files through the import service as the seeded ``admin``
  (``make demo``: ``data/case/source/case2_data.docx``); idempotent by content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from qost_api.settings import ApiSettings
    from twin_core.clock import Clock
    from twin_core.config import TwinConfig

DEFAULT_PORT = 8000
SERVICE = "api"


def cmd_serve(_args: argparse.Namespace) -> int:
    uvicorn.run(
        "qost_api.app:create_app",
        factory=True,
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", DEFAULT_PORT)),
        proxy_headers=True,
        log_level=os.environ.get("API_LOG_LEVEL", "info"),
    )
    return 0


def _sessions(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    from qost_api.db import make_engine, make_sessionmaker

    engine = make_engine(url)
    return engine, make_sessionmaker(engine)


def cmd_seed(_args: argparse.Namespace) -> int:
    from qost_api.seed import run_seed, seed_around
    from qost_api.settings import ApiSettings
    from twin_core.clock import SystemClock
    from twin_core.health import load_config_or_exit
    from twin_core.log import configure_logging

    log = configure_logging(SERVICE)
    cfg = load_config_or_exit(SERVICE)
    settings = ApiSettings()
    if not settings.database_url:
        print("make seed: DATABASE_URL is not set", file=sys.stderr)
        return 2

    async def run() -> dict[str, object]:
        engine, sessions = _sessions(str(settings.database_url))
        try:
            clock = SystemClock()
            report = await run_seed(
                sessions,
                cfg,
                settings,
                around=seed_around(cfg, settings, clock),
                now=_seed_now(cfg, settings, clock),
            )
            return report.as_dict()
        finally:
            await engine.dispose()

    report = asyncio.run(run())
    log.info("seed_done", **report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _seed_now(cfg: TwinConfig, settings: ApiSettings, clock: Clock) -> datetime:
    """Audit time of the seed: demo start in sim mode (plant time), else now."""
    if settings.clock_mode == "sim":
        return cfg.simulation.clock.demo_start
    return clock.now()


def cmd_import(args: argparse.Namespace) -> int:
    from qost_api.auth import Principal
    from qost_api.imports import create_import
    from qost_api.settings import ApiSettings
    from qost_api.users import find_user
    from twin_core.clock import ManualClock, SystemClock
    from twin_core.health import load_config_or_exit
    from twin_core.importer import UploadedFile
    from twin_core.log import configure_logging

    log = configure_logging(SERVICE)
    cfg = load_config_or_exit(SERVICE)
    settings = ApiSettings()
    if not settings.database_url:
        print("import: DATABASE_URL is not set", file=sys.stderr)
        return 2
    files = [UploadedFile(Path(p).name, Path(p).read_bytes()) for p in args.files]

    async def run() -> dict[str, object]:
        engine, sessions = _sessions(str(settings.database_url))
        try:
            async with sessions() as session:
                user = await find_user(session, args.user)
                if user is None:
                    raise SystemExit(f"import: user '{args.user}' does not exist (run make seed)")
                principal = Principal(user.username, user.role, user.id, user.display_name)
                clock = SystemClock()
                outcome = await create_import(
                    session,
                    files,
                    cfg=cfg,
                    clock=ManualClock(_seed_now(cfg, settings, clock)),
                    principal=principal,
                )
                result = outcome.job.result or {}
                return {
                    "import_id": outcome.job.id,
                    "created": outcome.created,
                    "dq_issues": len(result.get("data_quality_issues", [])),
                    "alerts": len(result.get("alerts", [])),
                }
        finally:
            await engine.dispose()

    summary = asyncio.run(run())
    log.info("import_done", **summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qost_api")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="HTTP + WebSocket API (default)")
    sub.add_parser("seed", help="users, reference tables, calendar, plan (make seed)")
    imp = sub.add_parser("import", help="import case files as a seeded user")
    imp.add_argument("files", nargs="+")
    imp.add_argument("--user", default="admin")
    args = parser.parse_args(argv)
    handlers = {"serve": cmd_serve, None: cmd_serve, "seed": cmd_seed, "import": cmd_import}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
