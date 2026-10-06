"""``/api/v1/import`` — file import (SPEC §7.4, §12.2; roles admin, director)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from qost_api.auth import Principal, require_roles
from qost_api.db import Session
from qost_api.imports import create_import, get_job, job_view
from qost_api.problems import ProblemError
from qost_api.settings import ApiSettings
from twin_core.clock import Clock
from twin_core.config import TwinConfig
from twin_core.importer import UploadedFile, build_template

IMPORT_ROLES = ("admin", "director")
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEMPLATE_NAME = "qost_import_template.xlsx"

router = APIRouter(prefix="/api/v1/import", tags=["import"])
# The principal is declared before the session: roles are checked before the database is touched.
Importer = Annotated[Principal, Depends(require_roles(*IMPORT_ROLES))]


@router.get(
    "/template.xlsx",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}}},
    summary="Import template (FR-IMP-07)",
)
async def import_template(request: Request, principal: Importer) -> Response:
    config: TwinConfig = request.app.state.config
    return Response(
        build_template(config),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_NAME}"'},
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Import .docx / .xlsx / .csv files (FR-IMP-01..06)",
    responses={200: {"description": "Same content imported before: the stored result"}},
)
async def post_import(
    request: Request,
    principal: Importer,
    session: Session,
    files: Annotated[list[UploadFile], File(description="one .docx, one .xlsx, or .csv/.txt/.zip")],
) -> JSONResponse:
    settings: ApiSettings = request.app.state.settings
    uploads: list[UploadedFile] = []
    total = 0
    for upload in files:
        content = await upload.read(settings.import_max_bytes + 1)
        total += len(content)
        if total > settings.import_max_bytes:
            raise ProblemError(
                413,
                "Upload too large",
                f"the upload exceeds {settings.import_max_bytes} bytes",
                slug="upload-too-large",
            )
        uploads.append(UploadedFile(upload.filename or "upload", content))
    clock: Clock = request.app.state.clock
    outcome = await create_import(
        session, uploads, cfg=request.app.state.config, clock=clock, principal=principal
    )
    body: dict[str, Any] = job_view(outcome.job)
    body["job"]["created"] = outcome.created
    return JSONResponse(
        body,
        status_code=status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK,
        headers={"Location": f"{router.prefix}/{outcome.job.id}"},
    )


@router.get("/{import_id}", summary="Import report (FR-IMP-05)")
async def get_import(import_id: int, principal: Importer, session: Session) -> dict[str, Any]:
    job = await get_job(session, import_id)
    if job is None:
        raise ProblemError(404, "Not Found", f"import {import_id} does not exist", slug="not-found")
    return job_view(job)
