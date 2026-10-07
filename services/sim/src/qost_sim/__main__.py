"""Entry point: ``python -m qost_sim [live|backfill|ml-dataset|tagmap|calibrate]``.

* ``live`` (default, the compose service): OPC UA :4840, MQTT UNS, demo console :8100,
  plant clock in Redis.
* ``backfill --sink jsonl:PATH``: history backfill_from -> demo_start into an event sink.
* ``ml-dataset --out DIR``: raw PdM dataset (parquet).
* ``tagmap --out config/tag_map.demo.yaml``: tag map from the OPC UA address space.
* ``calibrate``: calibration report against simulation.yaml targets (FR-SIM-03).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from qost_sim.address_space import build_address_space, load_contract, render_tag_map, state_codes
from qost_sim.model.plant import ModelConfigError, compile_signal_models
from qost_sim.model.scenarios import check_configured_scenarios
from qost_sim.settings import SimSettings
from twin_core.config import TwinConfig
from twin_core.health import load_config_or_exit
from twin_core.log import configure_logging

SERVICE = "sim"


def load_checked_config() -> TwinConfig:
    """Config + simulator-specific checks; any problem exits with code 2 (FR-DOM-01)."""
    cfg = load_config_or_exit(SERVICE)
    problems = check_configured_scenarios(cfg)
    try:
        compile_signal_models(cfg)
    except ModelConfigError as exc:
        problems.append(str(exc))
    if problems:
        print(f"[{SERVICE}] refusing to start:\n  " + "\n  ".join(problems), file=sys.stderr)
        raise SystemExit(2)
    return cfg


# --------------------------------------------------------------------------- live


async def serve_live(cfg: TwinConfig, settings: SimSettings) -> None:
    import uvicorn

    from qost_sim.bus import RedisBus
    from qost_sim.control_api import create_app
    from qost_sim.live import LiveRunner
    from qost_sim.mqtt_pub import MqttPublisher
    from qost_sim.opcua_server import OpcUaServer

    log = configure_logging(SERVICE)
    contract = load_contract(cfg)
    space = build_address_space(cfg)
    codes = state_codes(contract)
    opcua = OpcUaServer(
        space,
        endpoint=settings.sim_opcua_bind,
        namespace_uri=contract.opcua.namespace_uri,
        state_codes=codes,
    )
    mqtt = (
        MqttPublisher(
            cfg,
            space,
            url=settings.mqtt_url,
            topic_root=contract.mqtt.topic_root,
            state_codes=codes,
        )
        if settings.sim_mqtt
        else None
    )
    bus = RedisBus(settings.redis_url)
    runner = LiveRunner(cfg, settings, bus=bus, opcua=opcua, mqtt=mqtt)
    app = create_app(runner)

    async def run_runner() -> None:
        try:
            await runner.start_up()
        except Exception:
            runner.state = "error"
            log.exception("live_start_failed")
            return
        await runner.run()

    class Server(uvicorn.Server):
        """uvicorn without its own signal capture: it re-raises SIGTERM after shutdown, which
        would kill the process before the runner, OPC UA server and MQTT client are stopped."""

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    task = asyncio.create_task(run_runner(), name="live-runner")
    server = Server(
        uvicorn.Config(
            app,
            host=settings.sim_http_host,
            port=settings.sim_http_port,
            log_level="warning",
            lifespan="off",
        )
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
    log.info("sim_started", http_port=settings.sim_http_port, opcua=settings.sim_opcua_bind)
    try:
        await server.serve()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await runner.shutdown()
        await bus.aclose()
        log.info("sim_stopped")


# --------------------------------------------------------------------------- batch


def _instant(text: str | None) -> datetime | None:
    if text is None:
        return None
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise SystemExit(f"timestamp {text!r} needs a time zone (e.g. +05:00 or Z)")
    return value


def cmd_backfill(cfg: TwinConfig, args: argparse.Namespace) -> int:
    from qost_sim.backfill import run_backfill
    from twin_core.event_sink import open_sink

    async def main() -> Any:
        sink = open_sink(args.sink)
        try:
            return await run_backfill(
                cfg,
                sink,
                start=_instant(args.start),
                end=_instant(args.end),
                seed=args.seed,
                telemetry_period_s=args.telemetry_period,
            )
        finally:
            await sink.aclose()

    stats = asyncio.run(main())
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(stats.by_kind.items()))
    print(
        f"backfill {stats.start.isoformat()} .. {stats.end.isoformat()}: {stats.events} events "
        f"in {stats.batches} batches, {stats.seconds:.1f} s ({kinds})"
    )
    return 0


def cmd_ml_dataset(cfg: TwinConfig, args: argparse.Namespace) -> int:
    from qost_sim.ml_dataset import generate

    meta = generate(cfg, Path(args.out), months=args.months, seed=args.seed)
    print(f"ml-dataset -> {args.out}: {meta['rows']} ({meta['seconds']} s)")
    return 0


def cmd_tagmap(cfg: TwinConfig, args: argparse.Namespace) -> int:
    from qost_sim.address_space import write_tag_map

    contract = load_contract(cfg)
    endpoint = args.endpoint or contract.opcua.endpoint
    mqtt_url = args.mqtt_url or contract.mqtt.url
    out = Path(args.out)
    if args.check:
        text = render_tag_map(
            cfg, build_address_space(cfg), endpoint=endpoint, mqtt_url=mqtt_url, contract=contract
        )
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != text:
            print(f"{out} is out of date: run `make tagmap`", file=sys.stderr)
            return 1
        print(f"{out} is up to date")
        return 0
    tag_map = write_tag_map(cfg, out, endpoint=endpoint, mqtt_url=mqtt_url)
    print(f"tag map -> {out}: {len(tag_map.opcua.nodes)} nodes")
    return 0


def cmd_calibrate(cfg: TwinConfig, args: argparse.Namespace) -> int:
    from qost_sim.calibration import run_calibration

    report, _model, _records = run_calibration(
        cfg, seed=args.seed, warmup_days=args.warmup, days=args.days
    )
    print(report.render())
    return 1 if report.problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m qost_sim", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("live", help="run the virtual plant (default)")
    bf = sub.add_parser("backfill", help="write history backfill_from -> demo_start")
    bf.add_argument("--sink", required=True, help="jsonl:PATH | null: | (later) db:")
    bf.add_argument("--from", dest="start", help="override clock.backfill_from (ISO, with zone)")
    bf.add_argument("--to", dest="end", help="override clock.demo_start (ISO, with zone)")
    bf.add_argument("--seed", type=int)
    bf.add_argument("--telemetry-period", type=float, help="seconds (default: backfill period)")
    ml = sub.add_parser("ml-dataset", help="raw PdM dataset as parquet")
    ml.add_argument("--out", required=True)
    ml.add_argument("--months", type=int)
    ml.add_argument("--seed", type=int)
    tm = sub.add_parser("tagmap", help="generate config/tag_map.demo.yaml")
    tm.add_argument("--out", default="config/tag_map.demo.yaml")
    tm.add_argument("--endpoint", help="OPC UA endpoint clients use (default: example tag map)")
    tm.add_argument("--mqtt-url", help="MQTT broker URL (default: example tag map)")
    tm.add_argument("--check", action="store_true", help="fail if the file is out of date")
    cal = sub.add_parser("calibrate", help="calibration report (FR-SIM-03)")
    cal.add_argument("--seed", type=int)
    cal.add_argument("--warmup", type=int, default=5)
    cal.add_argument("--days", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "live"
    cfg = load_checked_config()
    if command == "live":
        asyncio.run(serve_live(cfg, SimSettings()))
        return 0
    handlers = {
        "backfill": cmd_backfill,
        "ml-dataset": cmd_ml_dataset,
        "tagmap": cmd_tagmap,
        "calibrate": cmd_calibrate,
    }
    return handlers[command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
