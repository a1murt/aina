"""Entry point: ``python -m qost_api [serve|seed|import FILE...|seed-reports]``.

* ``serve`` (default): uvicorn on :8000.
* ``seed``: ``make seed`` (FR-DB-01) — users of every role, reference tables, calendar, plan.
* ``import FILE...``: import plant source files through the import service as the seeded ``admin``
  (``make demo``: ``data/case/source/case2_data.docx``); idempotent by content.
* ``seed-reports --last N``: template shift reports for the last N closed shifts of plant time
  (``make demo`` after the replay), written by the internal ``system`` principal; idempotent.
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

    from qost_api.auth import Principal
    from qost_api.settings import ApiSettings
    from twin_core.clock import Clock, KeyValueStore
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


async def _plant_now(
    cfg: TwinConfig,
    settings: ApiSettings,
    at: str | None,
    *,
    store: KeyValueStore | None = None,
) -> datetime:
    """Plant time for a CLI command: ``--at``, the simulator clock in Redis, else the seed
    time (demo start in sim mode, wall time otherwise).

    A stale clock (the simulator stopped writing, e.g. after ``make demo-down`` with Redis
    still up) is not extrapolated: the stored ``plant_time`` is where the simulator resumes."""
    from twin_core.clock import RedisKV, SimClock, SystemClock, ensure_utc

    if at:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return ensure_utc(moment if moment.tzinfo else moment.replace(tzinfo=cfg.timezone))
    if settings.clock_mode == "sim" and (store is not None or settings.redis_enabled):
        sim = SimClock(store if store is not None else RedisKV.from_url(settings.redis_url))
        try:
            state = await sim.refresh()
            if state is not None:
                return state.plant_time if sim.is_stale else sim.now()
        except Exception:  # no Redis or no clock yet (redis errors are not OSError)
            pass
    return _seed_now(cfg, settings, SystemClock())


def cmd_seed_reports(args: argparse.Namespace) -> int:
    from qost_api.auth import SYSTEM_PRINCIPAL
    from qost_api.forecast.data import DbForecastBackend
    from qost_api.forecast.service import ForecastService
    from qost_api.llm import create_provider
    from qost_api.reports.data import DbReportBackend
    from qost_api.reports.seed import seed_reports
    from qost_api.reports.service import ReportService
    from qost_api.settings import ApiSettings
    from twin_core.clock import ManualClock
    from twin_core.forecast.result import ForecastResult
    from twin_core.health import load_config_or_exit
    from twin_core.log import configure_logging

    log = configure_logging(SERVICE)
    cfg = load_config_or_exit(SERVICE)
    settings = ApiSettings()
    if not settings.database_url:
        print("seed-reports: DATABASE_URL is not set", file=sys.stderr)
        return 2

    async def run() -> list[dict[str, object]]:
        engine, sessions = _sessions(str(settings.database_url))
        try:
            now = await _plant_now(cfg, settings, args.at)
            clock = ManualClock(now)
            forecasts = ForecastService(cfg, clock, DbForecastBackend(sessions))

            async def outlook(month: str, principal: Principal) -> ForecastResult | None:
                return await forecasts.outlook(month, principal)

            service = ReportService(
                cfg, clock, DbReportBackend(sessions), create_provider(), outlook
            )
            done = await seed_reports(
                service, cfg, now=now, last=args.last, lang=args.lang, principal=SYSTEM_PRINCIPAL
            )
            return [d.as_dict() for d in done]
        finally:
            await engine.dispose()

    reports = asyncio.run(run())
    log.info("seed_reports_done", reports=reports)
    print(json.dumps({"reports": reports}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qost_api")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="HTTP + WebSocket API (default)")
    sub.add_parser("seed", help="users, reference tables, calendar, plan (make seed)")
    imp = sub.add_parser("import", help="import plant source files as a seeded user")
    imp.add_argument("files", nargs="+")
    imp.add_argument("--user", default="admin")
    rep = sub.add_parser("seed-reports", help="template reports for the last closed shifts")
    rep.add_argument("--last", type=int, default=3, help="number of closed shifts (default 3)")
    rep.add_argument("--lang", choices=("ru", "kk"), default="ru")
    rep.add_argument("--at", default=None, help="plant time (ISO); default: the plant clock")
    args = parser.parse_args(argv)
    handlers = {
        "serve": cmd_serve,
        None: cmd_serve,
        "seed": cmd_seed,
        "import": cmd_import,
        "seed-reports": cmd_seed_reports,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
