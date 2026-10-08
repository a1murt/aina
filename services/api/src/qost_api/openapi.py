"""OpenAPI document and the offline Swagger UI (SPEC §12.1, rule 6).

* ``/api/openapi.json`` — FastAPI's schema plus, per operation, ``x-roles`` (the roles allowed by
  its role dependency, also appended to the description), the bearer security requirement and
  RFC 7807 responses (401/403/422/503, schema ``Problem``).
* ``/api/docs`` — Swagger UI 5 from the vendored assets in ``static/swagger`` (no CDN, the
  validator badge is off), so the documentation works with ``OFFLINE=true``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse

from qost_api.auth import ROLES_ATTR, roles_of
from qost_api.problems import PROBLEM_JSON

STATIC_DIR = Path(__file__).resolve().parent / "static"
DOCS_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.json"
_ASSETS = "/api/docs/assets"

PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "RFC 7807 problem details (application/problem+json)",
    "required": ["type", "title", "status"],
    "properties": {
        "type": {"type": "string", "example": "/problems/forbidden"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
}
_PROBLEM_RESPONSES = {
    "401": "Missing, invalid or expired token",
    "403": "The role may not call this endpoint",
    "422": "Request validation failed",
    "503": "Database / Redis / plant clock not available",
}


def _role_dependencies(dependant: Dependant | None) -> list[Any]:
    found: list[Any] = []
    stack = list(dependant.dependencies) if dependant is not None else []
    while stack:
        dep = stack.pop()
        if dep.call is not None and hasattr(dep.call, ROLES_ATTR):
            found.append(dep.call)
        stack.extend(dep.dependencies)
    return found


def route_roles(app: FastAPI) -> dict[tuple[str, str], list[str] | None]:
    """``(METHOD, path) -> allowed roles`` (``None``: public) of every HTTP route."""
    config = getattr(app.state, "config", None)
    out: dict[tuple[str, str], list[str] | None] = {}
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        deps = _role_dependencies(ctx.dependant)
        roles = roles_of(deps[0], config) if deps else None
        for method in ctx.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            out[(method, str(ctx.path))] = roles
    return out


def install_openapi(app: FastAPI) -> None:
    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components.setdefault("schemas", {})["Problem"] = PROBLEM_SCHEMA
        roles = route_roles(app)
        for path, item in schema.get("paths", {}).items():
            for method, op in item.items():
                allowed = roles.get((method.upper(), path))
                responses = op.setdefault("responses", {})
                if allowed is None:
                    op["x-roles"] = []
                    continue
                op["x-roles"] = allowed
                note = f"Roles: {', '.join(allowed)}."
                op["description"] = f"{op.get('description', '').strip()}\n\n{note}".strip()
                for code, text in _PROBLEM_RESPONSES.items():
                    responses.setdefault(
                        code,
                        {
                            "description": text,
                            "content": {
                                PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}
                            },
                        },
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def install_docs(app: FastAPI) -> None:
    """Offline Swagger UI at ``/api/docs`` (assets vendored under ``static/swagger``)."""
    app.mount(_ASSETS, StaticFiles(directory=STATIC_DIR / "swagger"), name="swagger-assets")

    @app.get(DOCS_URL, include_in_schema=False)
    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=OPENAPI_URL,
            title=f"{app.title} — API",
            swagger_js_url=f"{_ASSETS}/swagger-ui-bundle.js",
            swagger_css_url=f"{_ASSETS}/swagger-ui.css",
            swagger_favicon_url=f"{_ASSETS}/favicon-32x32.png",
            swagger_ui_parameters={
                "validatorUrl": None,
                "persistAuthorization": True,
                "displayRequestDuration": True,
                "docExpansion": "none",
                "tryItOutEnabled": True,
            },
        )
