"""Pre-show check of the live demo stand (STAB-5): one line per check, OK / WARN / FAIL, exit 1 on
any FAIL. ``make demo-check`` runs it inside the ``api`` container (stdlib + redis-py; the
services are reached by their compose names):

    make demo-check                      # after: restart engine -> make demo-reset
    make demo-check DEMO_MTD=2511        # also compare the month-to-date output at demo_start

Checks: /readyz or /healthz of sim, collector, engine, api, notifier, web; the engine reads the
event stream (``processed`` grows over a few seconds while the plant runs, consumer-group lag 0
while it is paused); the collector spool is empty; the plant clock is fresh and, before the show,
paused at ``demo_start``; the engine has acknowledged the simulator's reset epoch; the month has a
plan and MTD is within tolerance.

Service URLs: ``DEMO_CHECK_<SIM|COLLECTOR|ENGINE|API|NOTIFIER|WEB>_URL``; Redis: ``REDIS_URL``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

SERVICES: dict[str, tuple[str, str]] = {
    "sim": ("http://sim:8100", "/readyz"),
    "collector": ("http://collector:8110", "/readyz"),
    "engine": ("http://engine:8120", "/readyz"),
    "api": ("http://127.0.0.1:8000", "/healthz"),
    "notifier": ("http://notifier:8130", "/healthz"),
    "web": ("http://web:3000", "/healthz"),
}
RESTART_ENGINE = "docker compose -p qost restart engine && make demo-reset"


@dataclass(frozen=True)
class Line:
    status: str  # OK | WARN | FAIL
    name: str
    text: str

    def render(self) -> str:
        return f"{self.status:<4} {self.name:<10} {self.text}"


def ok(name: str, text: str) -> Line:
    return Line("OK", name, text)


def warn(name: str, text: str) -> Line:
    return Line("WARN", name, text)


def fail(name: str, text: str) -> Line:
    return Line("FAIL", name, text)


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def plant_tz(sim: Mapping[str, Any] | None) -> tzinfo:
    """The plant zone as the simulator reports it (``plant_time_local``), else UTC."""
    moment = parse_ts(sim.get("plant_time_local")) if sim else None
    return moment.tzinfo if moment is not None and moment.tzinfo is not None else UTC


def local(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m %H:%M")


# ---------------------------------------------------------------------------- pure checks


def check_health(name: str, status: int | None, body: Any, error: str | None) -> Line:
    if status is None:
        hint = f" — контейнер на паузе или завис: docker compose -p qost restart {name}"
        return fail(name, f"не отвечает ({error}){hint}")
    if status != 200:
        detail = ""
        if isinstance(body, dict) and body.get("not_ready"):
            detail = f" ({', '.join(map(str, body['not_ready']))})"
        return fail(name, f"HTTP {status}{detail}")
    return ok(name, "готов")


def check_engine(
    sim_running: bool,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    group_lag: int | None,
    waited_s: float,
) -> list[Line]:
    if before is None or after is None:
        return [fail("engine", f"/stats недоступен — {RESTART_ENGINE}")]
    out: list[Line] = []
    dead = [k for k, alive in (after.get("tasks_alive") or {}).items() if not alive]
    if dead:
        out.append(fail("engine", f"остановлены задачи {', '.join(dead)} — {RESTART_ENGINE}"))
    p0, p1 = int(before.get("processed") or 0), int(after.get("processed") or 0)
    lag = group_lag if group_lag is not None else after.get("group_lag")
    if sim_running:
        if p1 > p0:
            out.append(ok("engine", f"читает поток: +{p1 - p0} событий за {waited_s:.0f} с"))
        else:
            out.append(
                fail(
                    "engine",
                    f"processed не растёт ({p1}) при идущем симуляторе, лаг {lag} — "
                    f"{RESTART_ENGINE}",
                )
            )
    elif lag is None:
        out.append(warn("engine", "лаг группы events неизвестен (нет потока или XINFO)"))
    elif int(lag) == 0:
        out.append(ok("engine", "поток прочитан полностью (лаг 0), симулятор на паузе"))
    else:
        out.append(fail("engine", f"лаг группы events {lag} на паузе — {RESTART_ENGINE}"))
    return out


def check_collector(status: Mapping[str, str] | None) -> Line:
    if not status:
        return warn("collector", "нет collector:status в Redis")
    queued, spooled = int(status.get("queued", "0") or 0), int(status.get("spooled", "0") or 0)
    if queued or spooled:
        return fail(
            "collector", f"в очереди {queued}, в спуле {spooled} — запись в БД/Redis отстаёт"
        )
    return ok("collector", "очередь и спул пусты")


def check_clock(
    sim: Mapping[str, Any] | None,
    clock: Mapping[str, Any] | None,
    wall_now: datetime,
    *,
    max_drift_h: float,
    stale_s: float = 10.0,
) -> list[Line]:
    if sim is None:
        return [fail("clock", "статус симулятора недоступен")]
    demo = parse_ts(sim.get("demo_start"))
    plant = parse_ts(sim.get("plant_time"))
    if demo is None or plant is None:
        return [fail("clock", "симулятор не готов (нет plant_time)")]
    out: list[Line] = []
    tz = plant_tz(sim)
    running = not sim.get("paused", True)
    if clock is not None and running:
        wall_ts = parse_ts(clock.get("wall_ts"))
        if wall_ts is None or (wall_now - wall_ts).total_seconds() > stale_s:
            out.append(fail("clock", "plant:clock не обновляется — симулятор завис"))
    if plant - demo > timedelta(hours=max_drift_h):
        out.append(
            fail(
                "clock",
                f"стенд уехал: заводское время {local(plant, tz)} (старт {local(demo, tz)}) — "
                "make demo-reset",
            )
        )
    elif not running and abs((plant - demo).total_seconds()) <= 60:
        out.append(ok("clock", f"{local(plant, tz)}, пауза — готов к показу"))
    elif running:
        out.append(
            warn("clock", f"симулятор идёт ({local(plant, tz)}) — перед выходом make demo-reset")
        )
    else:
        out.append(warn("clock", f"пауза не на старте ({local(plant, tz)}) — make demo-reset"))
    return out


def check_epoch(sim: Mapping[str, Any] | None, engine: Mapping[str, Any] | None) -> Line:
    sim_epoch = sim.get("epoch") if sim else None
    eng_epoch = engine.get("epoch") if engine else None
    last = sim.get("last_reset") if sim else None
    if last not in (None, "acked"):
        return fail("epoch", f"последний сброс: {last} — движок не подтвердил; {RESTART_ENGINE}")
    if engine is None:
        return warn("epoch", f"эпоха движка неизвестна (/stats недоступен), симулятор {sim_epoch}")
    if eng_epoch is None:
        return warn("epoch", f"движок не видел сброса после запуска (эпоха симулятора {sim_epoch})")
    if sim_epoch != eng_epoch:
        return fail("epoch", f"симулятор {sim_epoch}, движок {eng_epoch} — {RESTART_ENGINE}")
    return ok("epoch", f"эпоха {sim_epoch}, сброс подтверждён движком")


def check_plan(
    progress: Mapping[str, Any] | None,
    *,
    at_start: bool,
    mtd_expect: float | None,
    mtd_tol: float,
) -> Line:
    if progress is None:
        return warn("plan", "GET /plan/progress недоступен (вход не выполнен)")
    targets = progress.get("targets") or {}
    mtd = progress.get("mtd_output")
    if not targets:
        return fail("plan", f"нет плана на месяц {progress.get('month')} — make demo-reset")
    if mtd_expect is not None and at_start and mtd is not None:
        if abs(float(mtd) - mtd_expect) > mtd_tol * mtd_expect:
            return fail("plan", f"MTD {mtd} авто, ожидалось ≈ {mtd_expect:g} — история не та?")
        return ok("plan", f"месяц {progress.get('month')}, MTD {mtd} авто (≈ {mtd_expect:g})")
    return ok("plan", f"месяц {progress.get('month')}, MTD {mtd} авто")


# ---------------------------------------------------------------------------- I/O


def url_of(name: str) -> str:
    return os.environ.get(f"DEMO_CHECK_{name.upper()}_URL", SERVICES[name][0]).rstrip("/")


def http_json(
    url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None
) -> tuple[int | None, Any, str | None]:
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (OSError, ValueError) as exc:
        return None, None, str(getattr(exc, "reason", exc))[:80]
    try:
        return status, json.loads(raw or b"null"), None
    except ValueError:
        return status, None, None


def redis_client() -> Any:
    try:
        from redis import Redis
    except ImportError:
        return None
    return Redis.from_url(
        os.environ.get("REDIS_URL", "redis://redis:6379/0"), socket_timeout=3, socket_connect_timeout=3
    )


def group_lag(redis: Any, stream: str = "events", group: str = "engine") -> int | None:
    if redis is None:
        return None
    try:
        for info in redis.xinfo_groups(stream):
            name = info.get("name")
            name = name.decode() if isinstance(name, bytes) else name
            if name == group and info.get("lag") is not None:
                return int(info["lag"])
    except Exception:
        return None
    return None


def api_progress(user: str, password: str | None) -> Mapping[str, Any] | None:
    if not password:
        return None
    base = url_of("api")
    status, body, _ = http_json(
        f"{base}/api/v1/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    if status != 200 or not isinstance(body, dict):
        return None
    token = body.get("access_token")
    status, progress, _ = http_json(
        f"{base}/api/v1/plan/progress", headers={"Authorization": f"Bearer {token}"}
    )
    return progress if status == 200 and isinstance(progress, dict) else None


def run(args: argparse.Namespace, now: Callable[[], datetime]) -> list[Line]:
    lines: list[Line] = []
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
        futures = {
            name: pool.submit(http_json, url_of(name) + path)
            for name, (_base, path) in SERVICES.items()
        }
        for name, fut in futures.items():
            status, body, error = fut.result()
            lines.append(check_health(name, status, body, error))
    _, sim, _ = http_json(url_of("sim") + "/status")
    sim = sim if isinstance(sim, dict) else None
    redis = redis_client()
    clock: Mapping[str, Any] | None = None
    collector: Mapping[str, str] | None = None
    if redis is not None:
        try:
            raw = redis.get("plant:clock")
            clock = json.loads(raw) if raw else None
            status = redis.hgetall("collector:status")
            collector = {k.decode(): v.decode() for k, v in status.items()}
        except Exception as exc:
            lines.append(fail("redis", f"недоступен: {str(exc)[:80]}"))
    running = bool(sim) and not sim.get("paused", True)
    _, before, _ = http_json(url_of("engine") + "/stats")
    waited = 0.0
    after = before
    if running and isinstance(before, dict):
        t0 = time.monotonic()
        time.sleep(args.wait_s)
        waited = time.monotonic() - t0
        _, after, _ = http_json(url_of("engine") + "/stats")
    before = before if isinstance(before, dict) else None
    after = after if isinstance(after, dict) else None
    lines += check_engine(running, before, after, group_lag(redis), waited)
    lines.append(check_collector(collector))
    lines += check_clock(sim, clock, now(), max_drift_h=args.max_drift_h)
    lines.append(check_epoch(sim, after))
    at_start = bool(sim) and not running and sim.get("plant_time") is not None
    if at_start and sim is not None:
        demo, plant = parse_ts(sim.get("demo_start")), parse_ts(sim.get("plant_time"))
        at_start = demo is not None and plant is not None and abs((plant - demo).total_seconds()) <= 60
    progress = api_progress(args.user, os.environ.get("DEMO_PASSWORD"))
    lines.append(
        check_plan(progress, at_start=at_start, mtd_expect=args.mtd_expect, mtd_tol=args.mtd_tol)
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_check", description=__doc__.split("\n")[0])
    parser.add_argument("--wait-s", type=float, default=5.0, help="engine progress window")
    parser.add_argument("--max-drift-h", type=float, default=10.0, help="plant time past demo_start")
    parser.add_argument("--mtd-expect", type=float, default=None, help="MTD output at demo_start")
    parser.add_argument("--mtd-tol", type=float, default=0.02, help="relative MTD tolerance")
    parser.add_argument("--user", default="director", help="API user for /plan/progress")
    args = parser.parse_args(argv)
    began = time.monotonic()
    lines = run(args, lambda: datetime.now(UTC))
    for line in lines:
        print(line.render(), flush=True)
    failed = [line for line in lines if line.status == "FAIL"]
    verdict = "FAIL" if failed else "OK"
    print(f"{verdict:<4} {'итог':<10} {len(failed)} ошибок, {time.monotonic() - began:.1f} с")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
