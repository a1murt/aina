"""Read-only copilot tools (SPEC §11.5): the same query functions the REST endpoints use.

Every tool calls the route function of the matching ``GET`` endpoint with the caller's
principal, so the data is exactly what the caller would get from the API; the tool set itself is
filtered by role (:data:`TOOLS`). Results are compacted (rounded numbers, truncated lists) so a
tool answer stays within a few thousand characters. Tool arguments come from a model: they are
validated here, errors become a short message the model can read (never an exception).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.auth import Principal
from qost_api.deps import Page
from qost_api.llm import ToolSpec
from qost_api.problems import ProblemError
from qost_api.queries.limits import limit_forecasts
from qost_api.routes import alerts as alerts_routes
from qost_api.routes import bottleneck as bottleneck_routes
from qost_api.routes import defects as defects_routes
from qost_api.routes import downtime as downtime_routes
from qost_api.routes import equipment as equipment_routes
from qost_api.routes import kpi as kpi_routes
from qost_api.routes import quality as quality_routes
from twin_core.clock import Clock
from twin_core.config import TwinConfig

MAX_RESULT_CHARS = 7000
MAX_ROWS = 20


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    roles: frozenset[str]

    def spec(self) -> ToolSpec:
        return ToolSpec(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
        )


_DATE = {"type": "string", "description": "YYYY-MM-DD, plant-local date"}
_PERIOD = {"from": _DATE, "to": {**_DATE, "description": "YYYY-MM-DD, inclusive"}}
ALL_COPILOT = frozenset({"director", "master", "maintenance", "quality"})


def _roles(*names: str) -> frozenset[str]:
    return frozenset(names)


TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "get_kpi",
        "KPI (OEE, availability, effectiveness, quality ratio, PQ/GQ, defect rate, MTBF/MTTR) of a "
        "line, area, the plant or a unit for a period. Default period: month start .. today; the "
        "running shift is included.",
        {
            "level": {"type": "string", "enum": ["line", "area", "plant", "equipment"]},
            "code": {"type": "string", "description": "entity code, e.g. PAINT-1, WELD, ABB-04"},
            **_PERIOD,
            "granularity": {"type": "string", "enum": ["shift", "day", "month"]},
        },
        (),
        ALL_COPILOT,
    ),
    ToolDef(
        "get_losses",
        "Loss tree: minutes, cars and tenge lost by category (downtime, starvation, blocking, "
        "microstops, speed, defects) for a period.",
        {
            "level": {"type": "string", "enum": ["plant", "area", "line"]},
            "code": {"type": "string"},
            **_PERIOD,
        },
        (),
        _roles("director", "master"),
    ),
    ToolDef(
        "get_downtime",
        f"Downtime records (newest first, at most {MAX_ROWS}) with reasons, durations and lines.",
        {
            "line": {"type": "string"},
            "entity": {"type": "string", "description": "unit or line code"},
            "reason_code": {"type": "string"},
            "planned": {"type": "boolean"},
            "open_only": {"type": "boolean", "description": "only stops that are running now"},
            **_PERIOD,
        },
        (),
        _roles("director", "master", "maintenance"),
    ),
    ToolDef(
        "get_defects",
        "Defects: counts by defect code with the cumulative share (Pareto) and the newest records.",
        {
            "area": {"type": "string"},
            "line": {"type": "string"},
            "defect_code": {"type": "string"},
            **_PERIOD,
        },
        (),
        _roles("director", "master", "quality"),
    ),
    ToolDef(
        "get_alerts",
        f"Alerts (newest first, at most {MAX_ROWS}) with severity, entity and message.",
        {
            "status": {"type": "string", "enum": ["open", "ack", "resolved"]},
            "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
            "rule_id": {"type": "string", "description": "e.g. AL-M1 failure risk, AL-M2 limit"},
            "entity": {"type": "string"},
            **_PERIOD,
        },
        (),
        ALL_COPILOT,
    ),
    ToolDef(
        "get_forecast",
        "Forecast of the month output (P10/P50/P90, probability of the plan and of the target, "
        "shortfall, required rate per shift). Default month: the current one.",
        {"month": {"type": "string", "description": "YYYY-MM"}},
        (),
        _roles("director"),
    ),
    ToolDef(
        "get_equipment_health",
        "Health of one unit: state, failure probability within 8 h with reasons, health index, "
        "signal values and time to the limit of signals (filters, chain), MTBF/MTTR.",
        {"code": {"type": "string", "description": "unit code, e.g. ABB-04, BOOTH-02"}},
        ("code",),
        _roles("director", "master", "maintenance"),
    ),
    ToolDef(
        "get_bottleneck",
        "Bottleneck: share of time each line was the sole or shifting bottleneck, and the "
        "current one.",
        dict(_PERIOD),
        (),
        _roles("director", "master"),
    ),
)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tools_for_role(role: str) -> tuple[ToolDef, ...]:
    return tuple(t for t in TOOLS if role in t.roles)


# --------------------------------------------------------------------------- compaction


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_round(v) for v in value]
    return value


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        out = [_truncate(v, limit) for v in value[:limit]]
        if len(value) > limit:
            out.append({"_truncated": len(value) - limit})
        return out
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "…"
    return value


def compact(value: Any, max_chars: int = MAX_RESULT_CHARS) -> str:
    """JSON text of ``value`` within ``max_chars`` (lists are cut progressively)."""
    value = _round(value)
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    for limit in (40, 20, 10, 5, 2):
        if len(text) <= max_chars:
            return text
        text = json.dumps(
            _truncate(value, limit), ensure_ascii=False, default=str, separators=(",", ":")
        )
    return text[:max_chars]


# --------------------------------------------------------------------------- execution


class ToolError(Exception):
    """A tool call the model got wrong (bad argument); the message goes back to the model."""


class Toolbox:
    """Executes tools for one caller. ``session`` is used sequentially."""

    def __init__(
        self,
        *,
        principal: Principal,
        request: Request,
        cfg: TwinConfig,
        clock: Clock,
        session: AsyncSession,
        forecast_service: Any | None = None,
    ) -> None:
        self.principal = principal
        self.request = request
        self.cfg = cfg
        self.clock = clock
        self.session = session
        self.forecast_service = forecast_service

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t.spec() for t in tools_for_role(self.principal.role))

    async def run(self, name: str, arguments: dict[str, Any]) -> Any:
        """The tool's result (JSON-able). Raises :class:`ToolError` for bad calls."""
        tool = TOOLS_BY_NAME.get(name)
        if tool is None or self.principal.role not in tool.roles:
            raise ToolError(f"tool '{name}' is not available for the role {self.principal.role}")
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        unknown = sorted(set(arguments) - set(tool.properties))
        if unknown:
            raise ToolError(
                f"unknown argument(s) {', '.join(unknown)}; known: {', '.join(tool.properties)}"
            )
        missing = [r for r in tool.required if not arguments.get(r)]
        if missing:
            raise ToolError(f"missing argument(s) {', '.join(missing)}")
        for key in ("from", "to"):
            value = arguments.get(key)
            if value is not None:
                try:
                    date.fromisoformat(str(value))
                except ValueError:
                    raise ToolError(f"'{key}' must be YYYY-MM-DD, got '{value}'") from None
        handler = getattr(self, f"_{name}")
        try:
            result = await handler(arguments)
        except ProblemError as exc:
            raise ToolError(exc.detail or exc.title) from exc
        return result

    # ------------------------------------------------------------------ tools

    def _page(self, limit: int = MAX_ROWS) -> Page:
        return Page(limit, None)

    async def _get_kpi(self, a: dict[str, Any]) -> Any:
        level = a.get("level", "line")
        granularity = a.get("granularity", "day")
        if level not in ("line", "area", "plant", "equipment"):
            raise ToolError("level must be line, area, plant or equipment")
        if granularity not in ("shift", "day", "month"):
            raise ToolError("granularity must be shift, day or month")
        return await kpi_routes.get_kpi(
            principal=self.principal,
            request=self.request,
            cfg=self.cfg,
            clock=self.clock,
            session=self.session,
            level=level,
            code=a.get("code"),
            start=a.get("from"),
            end=a.get("to"),
            granularity=granularity,
            include_live=True,
        )

    async def _get_losses(self, a: dict[str, Any]) -> Any:
        level = a.get("level", "plant")
        if level not in ("plant", "area", "line"):
            raise ToolError("level must be plant, area or line")
        return await kpi_routes.get_losses(
            principal=self.principal,
            request=self.request,
            cfg=self.cfg,
            clock=self.clock,
            session=self.session,
            level=level,
            code=a.get("code"),
            start=a.get("from"),
            end=a.get("to"),
            include_live=True,
        )

    async def _get_downtime(self, a: dict[str, Any]) -> Any:
        body = await downtime_routes.list_downtime(
            principal=self.principal,
            cfg=self.cfg,
            clock=self.clock,
            session=self.session,
            page=self._page(),
            line=a.get("line"),
            entity=a.get("entity"),
            entity_type=None,
            start=a.get("from"),
            end=a.get("to"),
            open_only=a.get("open_only"),
            planned=a.get("planned"),
            microstop=False,
            reason_code=a.get("reason_code"),
            reason_source=None,
            needs_classification=None,
        )
        for item in body["items"]:
            if isinstance(item.get("comment"), str):
                item["comment"] = item["comment"][:160]
        return {
            "items": body["items"],
            "count_shown": len(body["items"]),
            "more": body["next_cursor"] is not None,
        }

    async def _get_defects(self, a: dict[str, Any]) -> Any:
        pareto = await quality_routes.get_pareto(
            principal=self.principal,
            cfg=self.cfg,
            clock=self.clock,
            session=self.session,
            area=a.get("area"),
            line=a.get("line"),
            start=a.get("from"),
            end=a.get("to"),
        )
        recent = await defects_routes.list_defects(
            principal=self.principal,
            cfg=self.cfg,
            session=self.session,
            page=self._page(10),
            start=a.get("from"),
            end=a.get("to"),
            line=a.get("line"),
            area=a.get("area"),
            defect_code=a.get("defect_code"),
            source=None,
            body_id=None,
        )
        if a.get("defect_code"):
            pareto["items"] = [i for i in pareto["items"] if i["defect_code"] == a["defect_code"]]
        return {"pareto": pareto, "recent": recent["items"]}

    async def _get_alerts(self, a: dict[str, Any]) -> Any:
        body = await alerts_routes.list_alerts(
            principal=self.principal,
            cfg=self.cfg,
            session=self.session,
            page=self._page(),
            status=[a["status"]] if a.get("status") else None,
            severity=[a["severity"]] if a.get("severity") else None,
            rule_id=[a["rule_id"]] if a.get("rule_id") else None,
            entity=a.get("entity"),
            start=a.get("from"),
            end=a.get("to"),
            mine=False,
        )
        slim = [
            {k: i[k] for k in ("id", "ts", "rule_id", "severity", "entity", "status", "message_ru")}
            for i in body["items"]
        ]
        return {
            "items": slim,
            "open_counts": body.get("open_counts"),
            "more": body["next_cursor"] is not None,
        }

    async def _get_forecast(self, a: dict[str, Any]) -> Any:
        if self.forecast_service is None:
            raise ToolError("forecast is not available")
        month = a.get("month")
        result = await self.forecast_service.outlook(month, self.principal)
        data = result.model_dump(mode="json")
        return {
            "month": data["month"],
            "as_of": data["as_of"],
            "mtd": data["mtd"],
            "targets": data["targets"],
            "p_reach": data["p_reach"],
            "summary": data["summary"],
            "expected_shortfall": data["expected_shortfall"],
            "required_rate": data["required_rate"],
            "remaining_shifts": data.get("horizon", {}).get("remaining_shifts"),
        }

    async def _get_equipment_health(self, a: dict[str, Any]) -> Any:
        code = str(a["code"])
        if code not in self.cfg.equipment:
            raise ToolError(f"unknown unit '{code}' (known: {', '.join(self.cfg.equipment)})")
        body = await equipment_routes.get_health(
            code=code,
            principal=self.principal,
            request=self.request,
            cfg=self.cfg,
            clock=self.clock,
            session=self.session,
        )
        body["limits"] = body.get("limits") or await limit_forecasts(
            self.session, self.cfg, code, self.clock.now()
        )
        return body

    async def _get_bottleneck(self, a: dict[str, Any]) -> Any:
        body = await bottleneck_routes.get_bottleneck(
            principal=self.principal,
            request=self.request,
            cfg=self.cfg,
            clock=self.clock,
            session=self.session,
            start=a.get("from"),
            end=a.get("to"),
        )
        body["chronology"] = body["chronology"][-8:]
        return body
