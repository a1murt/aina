"""Entry point: ``python -m qost_engine [live|replay]``.

* ``live`` (default): consume the ``events`` stream (group ``engine``), keep the live state in
  Redis ``live:*``, publish deltas on ``live``, write derived tables (SPEC §9).
* ``replay [--from --to] [--jsonl PATH]``: recompute the derived tables of a period from
  ``event_raw`` (``make history``); the live engine continues from its final state.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from qost_engine.settings import EngineSettings
from twin_core.health import load_config_or_exit
from twin_core.log import configure_logging

SERVICE = "engine"
DEFAULT_PORT = 8120


def _instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise SystemExit(f"--from/--to need a zone: {value!r}")
    return moment


def cmd_replay(args: argparse.Namespace) -> int:
    from qost_engine.replay import run_replay
    from qost_engine.service import reset_stream_and_live

    configure_logging(SERVICE)
    cfg = load_config_or_exit(SERVICE)
    settings = EngineSettings()
    stats = asyncio.run(
        run_replay(
            cfg,
            database_url=settings.database_url,
            start=_instant(args.start),
            end=_instant(args.end),
            jsonl=Path(args.jsonl) if args.jsonl else None,
            resolve_history_alerts=settings.engine_replay_alerts == "resolved",
            checkpoint=settings.engine_checkpoint,
            line_state_source=settings.engine_line_state,
        )
    )
    if not args.keep_stream:
        asyncio.run(reset_stream_and_live(settings))
    print(
        f"replay {stats.start.isoformat()} .. {stats.end.isoformat()}: {stats.events} events, "
        f"{stats.rows} rows in {stats.seconds:.1f} s (read {stats.read_s:.1f}, core "
        f"{stats.core_s:.1f}, write {stats.write_s:.1f}); late {stats.core.get('late_events')}"
    )
    return 0


def cmd_live(_args: argparse.Namespace) -> int:
    from qost_engine.service import run_service

    asyncio.run(run_service())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qost_engine")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("live", help="stream processing (default)")
    rp = sub.add_parser("replay", help="recompute derived tables from event_raw")
    rp.add_argument("--from", dest="start", help="default: clock.backfill_from (ISO, with zone)")
    rp.add_argument("--to", dest="end", help="default: clock.demo_start (ISO, with zone)")
    rp.add_argument("--jsonl", help="read events from a JSONL file instead of event_raw")
    rp.add_argument(
        "--keep-stream", action="store_true", help="do not trim the events stream / live keys"
    )
    args = parser.parse_args(argv)
    handlers = {"replay": cmd_replay, "live": cmd_live, None: cmd_live}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
