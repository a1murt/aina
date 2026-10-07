"""Demo console HTTP API of the simulator (SPEC §6.9; profile ``demo``, proxied by ``api`` for
the ``admin`` role).

``GET /status`` · ``GET /scenarios`` · ``POST /start`` · ``POST /pause`` · ``POST /speed {value}``
· ``POST /inject {scenario_id}`` or ``{type, ...}`` · ``POST /reset {to: "demo_start"}``
· ``GET /healthz`` · ``GET /readyz``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from qost_sim.live import LiveRunner, RunnerError
from qost_sim.model.scenarios import InjectError, parse_inject


class SpeedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["demo_start"] = "demo_start"


def create_app(runner: LiveRunner) -> FastAPI:
    app = FastAPI(
        title="Qost Twin virtual plant console",
        version="1.0",
        docs_url=None,
        redoc_url=None,
    )
    cfg = runner.cfg

    def conflict(exc: RunnerError) -> HTTPException:
        return HTTPException(status_code=409, detail=str(exc))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "sim"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = runner.ready
        return JSONResponse(
            {"status": "ready" if ready else "not ready", "service": "sim", "state": runner.state},
            status_code=200 if ready else 503,
        )

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return runner.status()

    @app.get("/scenarios")
    async def scenarios() -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "name_ru": s.name_ru,
                "name_kk": s.name_kk,
                "at_min": s.at_min,
                "inject": s.inject.model_dump(mode="json"),
            }
            for s in cfg.simulation.scenarios
        ]

    @app.post("/start")
    async def start() -> dict[str, Any]:
        try:
            await runner.start()
        except RunnerError as exc:
            raise conflict(exc) from None
        return runner.status()

    @app.post("/pause")
    async def pause() -> dict[str, Any]:
        try:
            await runner.pause()
        except RunnerError as exc:
            raise conflict(exc) from None
        return runner.status()

    @app.post("/speed")
    async def speed(body: SpeedBody) -> dict[str, Any]:
        try:
            await runner.set_speed(body.value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RunnerError as exc:
            raise conflict(exc) from None
        return runner.status()

    @app.post("/inject")
    async def inject(body: dict[str, Any]) -> dict[str, Any]:
        scenario_id = body.get("scenario_id")
        if scenario_id is not None:
            if set(body) != {"scenario_id"}:
                raise HTTPException(
                    422, detail="send either {scenario_id} or an inject {type, ...}"
                )
            scenario = cfg.scenarios.get(str(scenario_id))
            if scenario is None:
                known = ", ".join(cfg.scenarios)
                raise HTTPException(
                    404, detail=f"unknown scenario '{scenario_id}' (known: {known})"
                )
            parsed = scenario.inject
        else:
            try:
                parsed = parse_inject(cfg, body)
            except InjectError as exc:
                raise HTTPException(422, detail=exc.problems) from None
        try:
            at = await runner.inject(parsed, scenario_id=scenario_id)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except RunnerError as exc:
            raise conflict(exc) from None
        return {"applied_at": at.isoformat(), "scenario_id": scenario_id, "status": runner.status()}

    @app.post("/reset")
    async def reset(body: ResetBody | None = None) -> dict[str, Any]:
        try:
            outcome = await runner.reset()
        except RunnerError as exc:
            raise conflict(exc) from None
        return {"cleanup": outcome, "status": runner.status()}

    return app
