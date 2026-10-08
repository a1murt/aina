"""``POST /api/v1/copilot/ask`` — questions about the plant (SPEC §11.5, §12.2).

Roles: director, master, maintenance, quality. The answer is built from read-only tools with the
caller's rights (at most 5 calls); with ``LLM_PROVIDER=none`` or ``OFFLINE=true`` a deterministic
router answers the canned question types (``mode: offline``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from qost_api.auth import Principal, require_roles
from qost_api.copilot.service import CopilotService
from qost_api.db import Session
from qost_api.llm import create_provider
from qost_api.problems import ProblemError

router = APIRouter(prefix="/api/v1/copilot", tags=["copilot"])
Asker = Annotated[Principal, Depends(require_roles("director", "master", "maintenance", "quality"))]


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    lang: Literal["ru", "kk"] | None = None


def get_copilot_service(request: Request) -> CopilotService:
    """Built on first use from ``app.state`` (tests set ``app.state.copilot_service``)."""
    state = request.app.state
    service: CopilotService | None = getattr(state, "copilot_service", None)
    if service is not None:
        return service
    if getattr(state, "sessionmaker", None) is None:
        raise ProblemError(
            503, "Database is not configured", "DATABASE_URL is not set", slug="no-database"
        )
    service = CopilotService(
        state.config,
        state.clock,
        create_provider(),
        forecast_service=getattr(state, "forecast_service", None),
    )
    state.copilot_service = service
    return service


@router.post("/ask", summary="Ask the copilot (read-only tools, role-filtered, ≤ 5 calls)")
async def ask(
    body: AskRequest, principal: Asker, request: Request, session: Session
) -> dict[str, Any]:
    service = get_copilot_service(request)
    lang = body.lang or (principal.lang if principal.lang in ("ru", "kk") else "ru")
    answer = await service.ask(
        question=body.question.strip(),
        principal=principal,
        lang=lang,
        request=request,
        session=session,
    )
    return answer.as_dict()
