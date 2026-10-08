"""REST routers under ``/api/v1`` (SPEC §12.2) and the WebSocket.

:data:`ROUTERS` is the single registry the app includes, in this order; a new stage adds its
router here (one line, no change to ``app.py``).
"""

from __future__ import annotations

from fastapi import APIRouter

from qost_api.live import ws
from qost_api.routes import (
    alerts,
    assets,
    auth,
    bodies,
    bottleneck,
    copilot,
    data_quality,
    defects,
    downtime,
    equipment,
    forecast,
    imports,
    kpi,
    live,
    predictions,
    quality,
    reports,
    service,
    sim,
    terminal,
    work_orders,
)

ROUTERS: tuple[APIRouter, ...] = (
    service.router,
    auth.router,
    assets.router,
    live.router,
    kpi.router,
    downtime.router,
    terminal.router,
    defects.router,
    alerts.router,
    data_quality.router,
    imports.router,
    forecast.router,
    bottleneck.router,
    equipment.router,
    sim.router,
    reports.router,
    copilot.router,
    predictions.router,
    work_orders.router,
    quality.router,
    bodies.router,
    ws.router,
)
