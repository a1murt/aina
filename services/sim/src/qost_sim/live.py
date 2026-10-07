"""Live mode: pace the deterministic model against the wall clock and publish it (SPEC §6.1).

* Warm-up: the same model as ``backfill`` runs silently from ``clock.backfill_from`` to
  ``clock.demo_start`` (in a thread), so the live state at demo_start is bit-identical to the end
  of the backfill history. ``at_min`` scenarios are scheduled relative to demo_start.
* Pacing (infra layer, the only place that reads wall time): plant time =
  anchor_plant + (monotonic() - anchor_wall) x speed; every tick the model runs up to it and the
  new records go to OPC UA / MQTT. ``plant:clock`` is written on every change and at 1 Hz.
* Interventions from the console are applied at the current plant time, logged and persisted in
  Redis (``sim:state``) together with the run epoch, so a restarted simulator replays to the last
  published plant time; ``reset`` rebuilds the model at demo_start with a new epoch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal

import structlog
from pydantic import TypeAdapter

from qost_sim.bus import ResetOutcome, SimBus
from qost_sim.model import EventFactory, Inject, PlantModel, Rec
from qost_sim.model.scenarios import check_inject
from qost_sim.mqtt_pub import MqttPublisher
from qost_sim.opcua_server import OpcUaServer
from qost_sim.settings import SimSettings
from twin_core.clock import ClockState, ClockStateError, format_utc, read_clock_state, system_now
from twin_core.config import TwinConfig
from twin_core.config.simulation import DefectMultiplierInject, FailureInject
from twin_core.config.simulation import Inject as InjectUnion

log = structlog.get_logger("qost_sim.live")

RunnerState = Literal["starting", "warming_up", "running", "paused", "resetting", "error"]
_INJECT: TypeAdapter[Inject] = TypeAdapter(InjectUnion)
_STATE_VERSION = 1
_MAX_LAG_WALL_S = 5.0


class RunnerError(RuntimeError):
    """A command that cannot run now (409)."""


def warm_model(
    cfg: TwinConfig,
    *,
    seed: int | None = None,
    telemetry_period_s: float | None = None,
    interventions: Sequence[tuple[datetime, Inject, str | None]] = (),
    resume_to: datetime | None = None,
    start: datetime | None = None,
    demo_start: datetime | None = None,
) -> PlantModel:
    """Model at ``demo_start`` (or ``resume_to``) — identical to the backfill's final state.

    ``start`` / ``demo_start`` default to ``clock.backfill_from`` / ``clock.demo_start``.
    """
    clock = cfg.simulation.clock
    demo = demo_start or clock.demo_start
    model = PlantModel(cfg, start=start or clock.backfill_from, seed=seed)
    model.run_until_time(demo)
    model.drain()
    if telemetry_period_s is not None:
        model.start_telemetry(telemetry_period_s)
    demo_s = model.sec(demo)
    for scenario in cfg.simulation.scenarios:
        if scenario.at_min is not None:
            model.schedule(
                demo_s + scenario.at_min * 60.0, scenario.inject, scenario_id=scenario.id
            )
    for at, inject, scenario_id in interventions:
        model.schedule(model.sec(at), inject, scenario_id=scenario_id)
    if resume_to is not None and resume_to > demo:
        model.run_until_time(resume_to)
        model.drain()
    return model


class LiveRunner:
    def __init__(
        self,
        cfg: TwinConfig,
        settings: SimSettings,
        *,
        bus: SimBus | None = None,
        opcua: OpcUaServer | None = None,
        mqtt: MqttPublisher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = system_now,
        speed: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = settings
        self.bus = bus
        self.opcua = opcua
        self.mqtt = mqtt
        self._mono = monotonic
        self._wall = wall
        clock = cfg.simulation.clock
        self.demo_start = clock.demo_start
        self.speed = float(speed if speed is not None else clock.speed)
        self.max_speed = max(clock.speed_presets)
        self.seed = clock.random_seed
        self.state: RunnerState = "starting"
        self.epoch = 0
        self.model: PlantModel | None = None
        self.factory: EventFactory | None = None
        self.manual: list[tuple[datetime, Inject, str | None]] = []
        self.anchor_plant = 0.0
        self.anchor_wall = 0.0
        self.last_reset: ResetOutcome | None = None
        self.published_records = 0
        self._lock = asyncio.Lock()
        self._last_clock_wall = -1e9
        self._last_shift = ""
        self.tick_s = settings.sim_tick_s

    # ================================================================== lifecycle

    @property
    def ready(self) -> bool:
        return self.state in ("running", "paused")

    async def start_up(self) -> None:
        """Restore or warm up the model, start the servers, publish the first values."""
        resume_to: datetime | None = None
        persisted = await self._load_state()
        if persisted is not None:
            self.epoch = int(persisted.get("epoch", 0))
            previous = await self._read_clock()
            if (
                self.settings.sim_resume
                and previous is not None
                and previous.sim_mode == "live"
                and previous.plant_time > self.demo_start
            ):
                resume_to = previous.plant_time
                self.manual = self._parse_interventions(persisted.get("interventions", []))
            else:
                self.epoch += 1
        self.state = "warming_up"
        await self._rebuild(resume_to)
        if self.opcua is not None:
            assert self.model is not None
            await self.opcua.start(self.model.now)
        if self.mqtt is not None:
            self.mqtt.start()
        await self._publish_snapshot()
        await self._save_state()
        self._anchor()
        self.state = "running" if self.settings.sim_autostart else "paused"
        await self._publish_clock(force=True)
        log.info(
            "live_ready",
            plant_time=format_utc(self.plant_time),
            epoch=self.epoch,
            resumed=resume_to is not None,
            state=self.state,
        )

    async def shutdown(self) -> None:
        if self.mqtt is not None:
            await self.mqtt.stop()
        if self.opcua is not None:
            await self.opcua.stop()

    async def run(self) -> None:
        """Pacing loop (until cancelled)."""
        while True:
            await asyncio.sleep(self.tick_s)
            async with self._lock:
                if self.state == "running":
                    await self._advance()
                await self._publish_clock()

    # ================================================================== time

    @property
    def plant_time(self) -> datetime:
        if self.model is None:
            return self.demo_start
        return self.model.now

    def _anchor(self) -> None:
        assert self.model is not None
        self.anchor_plant = self.model.env.now
        self.anchor_wall = self._mono()

    async def _advance(self) -> None:
        model = self.model
        assert model is not None
        wall = self._mono()
        target = self.anchor_plant + (wall - self.anchor_wall) * self.speed
        limit = model.env.now + self.speed * _MAX_LAG_WALL_S
        if target > limit:
            log.warning("live_lagging", behind_s=target - limit)
            target = limit
            self.anchor_plant, self.anchor_wall = target, wall
        model.run_until(target)
        await self._publish(model.drain())

    # ================================================================== publishing

    async def _publish(self, records: list[Rec]) -> None:
        model, factory = self.model, self.factory
        assert model is not None
        assert factory is not None
        if not records:
            return
        self.published_records += len(records)
        if self.opcua is not None:
            await self.opcua.write_records(model, records)
        if self.mqtt is not None:
            self.mqtt.publish_records(model, records, factory)

    async def _publish_snapshot(self) -> None:
        model = self.model
        assert model is not None
        ts = model.now
        if self.opcua is not None:
            await self.opcua.write_snapshot(model, ts)
        if self.mqtt is not None:
            self.mqtt.publish_snapshot(model, ts)

    def shift_label(self) -> str:
        model = self.model
        if model is None or model.shift is None:
            return ""
        return f"{model.shift.shift_date.isoformat()}/{model.shift.code}"

    async def _publish_clock(self, *, force: bool = False) -> None:
        wall = self._mono()
        if not force and wall - self._last_clock_wall < 1.0:
            return
        self._last_clock_wall = wall
        plant = self.plant_time
        paused = self.state != "running"
        site = self.cfg.plant.site.code
        shift = self.shift_label()
        speed = 0.0 if paused else self.speed
        if self.opcua is not None:
            await self.opcua.write_clock(site, plant, shift, speed)
        if self.mqtt is not None:
            self.mqtt.signal("site", site, "clock", plant, plant)
            if force or shift != self._last_shift:
                self.mqtt.signal("site", site, "shift", shift, plant)
                self.mqtt.signal("site", site, "speed", speed, plant)
        self._last_shift = shift
        if self.bus is not None:
            state = ClockState(plant, self._wall(), self.speed, paused=paused, sim_mode="live")
            try:
                await self.bus.set(self.settings.sim_clock_key, state.to_json())
            except (OSError, ConnectionError) as exc:  # Redis down: keep simulating
                log.warning("clock_publish_failed", error=str(exc))

    # ================================================================== commands

    def _require_ready(self) -> PlantModel:
        if not self.ready or self.model is None:
            raise RunnerError(f"simulator is {self.state}")
        return self.model

    async def start(self) -> None:
        async with self._lock:
            self._require_ready()
            if self.state == "paused":
                self._anchor()
                self.state = "running"
            await self._publish_clock(force=True)

    async def pause(self) -> None:
        async with self._lock:
            self._require_ready()
            if self.state == "running":
                await self._advance()
                self.state = "paused"
            await self._publish_clock(force=True)

    async def set_speed(self, value: float) -> None:
        if not 0 < value <= self.max_speed:
            raise ValueError(f"speed must be in (0, {self.max_speed:g}]")
        async with self._lock:
            self._require_ready()
            if self.state == "running":
                await self._advance()
            self.speed = float(value)
            self._anchor()
            await self._publish_clock(force=True)

    async def inject(self, inject: Inject, *, scenario_id: str | None = None) -> datetime:
        problems = check_inject(self.cfg, inject)
        if problems:
            raise ValueError("; ".join(problems))
        async with self._lock:
            model = self._require_ready()
            if self.state == "running":
                await self._advance()  # catch up to "now" so the inject lands at the paced time
            applied = model.now
            model.apply(inject, scenario_id=scenario_id)
            self.manual.append((applied, inject, scenario_id))
            await self._save_state()
            # process the inject right away (also while paused) so the console sees its effect
            model.run_until(model.env.now + 1e-6)
            await self._publish(model.drain())
            log.info("inject", scenario_id=scenario_id, type=inject.type, at=format_utc(applied))
            return applied

    async def reset(self) -> ResetOutcome:
        """Back to demo_start: new epoch, cleanup handshake, rebuilt model, republished values."""
        async with self._lock:
            self._require_ready()
            was_running = self.state == "running"
            self.state = "resetting"
            await self._publish_clock(force=True)
            await asyncio.sleep(self.settings.sim_reset_flush_s)
            self.epoch += 1
            self.manual = []
            outcome: ResetOutcome = "no_listeners"
            if self.bus is not None:
                try:
                    outcome = await self.bus.broadcast_reset(
                        self.settings.sim_control_channel,
                        {
                            "action": "reset",
                            "epoch": self.epoch,
                            "demo_start": format_utc(self.demo_start),
                        },
                        timeout_s=self.settings.sim_reset_ack_timeout_s,
                    )
                except (OSError, ConnectionError) as exc:
                    log.warning("reset_broadcast_failed", error=str(exc))
                    outcome = "error"
            self.last_reset = outcome
            await self._rebuild(None)
            await self._publish_snapshot()
            await self._save_state()
            self._anchor()
            self.state = "running" if was_running else "paused"
            await self._publish_clock(force=True)
            log.info("reset", epoch=self.epoch, cleanup=outcome)
            return outcome

    async def _rebuild(self, resume_to: datetime | None) -> None:
        period = self.cfg.simulation.telemetry.sample_period_s
        manual = list(self.manual)
        self.model = await asyncio.to_thread(
            warm_model,
            self.cfg,
            seed=self.seed,
            telemetry_period_s=period,
            interventions=manual,
            resume_to=resume_to,
        )
        self.factory = EventFactory.deterministic(
            site=self.cfg.plant.site.code,
            t0=self.model.t0,
            seed=self.seed,
            nonce=f"live:{self.epoch}",
        )

    # ================================================================== status

    def status(self) -> dict[str, Any]:
        model = self.model
        info: dict[str, Any] = {
            "state": self.state,
            "mode": "live",
            "ready": self.ready,
            "speed": self.speed,
            "speed_presets": list(self.cfg.simulation.clock.speed_presets),
            "paused": self.state != "running",
            "epoch": self.epoch,
            "seed": self.seed,
            "demo_start": format_utc(self.demo_start),
            "last_reset": self.last_reset,
        }
        if model is None:
            return info
        now = model.env.now
        plant = model.now
        info["plant_time"] = format_utc(plant)
        info["plant_time_local"] = plant.astimezone(model.tz).isoformat()
        info["shift"] = (
            None
            if model.shift is None
            else {"date": model.shift.shift_date.isoformat(), "code": model.shift.code}
        )
        scenarios = []
        for item in model.interventions:
            inject = item.inject
            duration = (
                inject.duration_min * 60.0
                if isinstance(inject, FailureInject | DefectMultiplierInject)
                else 0.0
            )
            if item.t > now:
                status = "scheduled"
            elif now < item.t + duration:
                status = "active"
            else:
                status = "applied"
            scenarios.append(
                {
                    "scenario_id": item.scenario_id,
                    "type": inject.type,
                    "at": format_utc(model.at(item.t)),
                    "status": status,
                    "inject": inject.model_dump(mode="json"),
                }
            )
        info["scenarios"] = scenarios
        info["lines"] = {
            code: {"state": ln.state.value, "reason": ln.reason, "produced": ln.produced}
            for code, ln in model.lines.items()
        }
        info["buffers"] = {
            code: {"level": b.level, "capacity": b.capacity} for code, b in model.buffers.items()
        }
        info["kits"] = dict(model.ckd.kits)
        return info

    # ================================================================== persistence

    async def _load_state(self) -> dict[str, Any] | None:
        if self.bus is None:
            return None
        try:
            raw = await self.bus.get(self.settings.sim_state_key)
        except (OSError, ConnectionError) as exc:
            log.warning("state_load_failed", error=str(exc))
            return None
        if raw is None:
            return None
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("v") == _STATE_VERSION:
                return data
        return None

    async def _read_clock(self) -> ClockState | None:
        if self.bus is None:
            return None
        try:
            return await read_clock_state(self.bus, key=self.settings.sim_clock_key)
        except (ClockStateError, OSError, ConnectionError):
            return None

    async def _save_state(self) -> None:
        if self.bus is None:
            return
        doc = {
            "v": _STATE_VERSION,
            "epoch": self.epoch,
            "interventions": [
                {"at": format_utc(at), "scenario_id": sid, "inject": inj.model_dump(mode="json")}
                for at, inj, sid in self.manual
            ],
        }
        try:
            await self.bus.set(self.settings.sim_state_key, json.dumps(doc, ensure_ascii=False))
        except (OSError, ConnectionError) as exc:
            log.warning("state_save_failed", error=str(exc))

    def _parse_interventions(
        self, items: list[dict[str, Any]]
    ) -> list[tuple[datetime, Inject, str | None]]:
        result: list[tuple[datetime, Inject, str | None]] = []
        for item in items:
            try:
                at = datetime.fromisoformat(str(item["at"]))
                inject = _INJECT.validate_python(item["inject"])
            except (KeyError, ValueError) as exc:
                log.warning("intervention_skipped", item=item, error=str(exc))
                continue
            result.append((at, inject, item.get("scenario_id")))
        return result
