"""Import API without a database: roles (JWT), RFC 7807 problems, template, helpers.

The database path (POST/GET with persistence, idempotency) is tested in
tests/integration/test_import_api.py.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import date

import openpyxl
import pytest
from fastapi.testclient import TestClient

from api_support import bearer
from qost_api.app import create_app
from qost_api.imports import upload_digest, upload_name
from qost_api.routes.imports import XLSX_MEDIA_TYPE
from support import CASE_DOCX
from twin_core.alert_text import alert_message_ru
from twin_core.config import TwinConfig
from twin_core.importer import UploadedFile
from twin_core.rules import Alert

PROBLEM = "application/problem+json"


@pytest.fixture
def client(cfg: TwinConfig) -> Iterator[TestClient]:
    with TestClient(create_app(cfg, database_url=None)) as test_client:
        yield test_client


def test_template_download(client: TestClient) -> None:
    response = client.get("/api/v1/import/template.xlsx", headers=bearer("director"))
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MEDIA_TYPE
    assert "qost_import_template.xlsx" in response.headers["content-disposition"]
    book = openpyxl.load_workbook(io.BytesIO(response.content))
    assert book.sheetnames[:4] == ["Линии", "Простои", "План", "Качество"]


@pytest.mark.parametrize("role", ["operator", "master", "maintenance", "quality"])
def test_other_roles_are_forbidden(client: TestClient, role: str) -> None:
    response = client.get("/api/v1/import/template.xlsx", headers=bearer(role))
    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["type"] == "/problems/forbidden"
    assert body["status"] == 403
    assert body["instance"] == "/api/v1/import/template.xlsx"


def test_unknown_role_is_forbidden(client: TestClient) -> None:
    response = client.post("/api/v1/import", headers=bearer("root"))
    assert response.status_code == 403
    assert "unknown role" in response.json()["detail"]


def test_credentials_are_required(cfg: TwinConfig) -> None:
    with TestClient(create_app(cfg, database_url=None)) as client:
        response = client.get("/api/v1/import/template.xlsx")
        assert response.status_code == 401
        assert response.headers["content-type"] == PROBLEM
        ok = client.get("/api/v1/import/template.xlsx", headers=bearer("admin"))
        assert ok.status_code == 200


def test_data_endpoints_need_a_database(client: TestClient) -> None:
    files = {"files": (CASE_DOCX.name, CASE_DOCX.read_bytes())}
    response = client.post("/api/v1/import", files=files, headers=bearer("admin"))
    assert response.status_code == 503
    assert response.json()["type"] == "/problems/no-database"
    assert client.get("/api/v1/import/1", headers=bearer("admin")).status_code == 503


def test_unknown_route_is_a_problem(client: TestClient) -> None:
    response = client.get("/api/v1/nope", headers=bearer("admin"))
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["title"] == "Not Found"


def test_openapi_lists_import_endpoints(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert {"/api/v1/import", "/api/v1/import/{import_id}", "/api/v1/import/template.xlsx"} <= set(
        paths
    )


def test_upload_digest_ignores_names_and_order() -> None:
    a, b = UploadedFile("a.csv", b"1"), UploadedFile("b.csv", b"2")
    assert upload_digest([a]) == "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b"
    assert upload_digest([a, b]) == upload_digest(
        [UploadedFile("x", b"2"), UploadedFile("y", b"1")]
    )
    assert upload_digest([a, b]) != upload_digest([a])
    assert upload_name([b, a]) == "a.csv, b.csv"


def test_alert_messages(cfg: TwinConfig) -> None:
    day = date(2026, 10, 2)
    q1 = Alert("AL-Q1", "critical", "area", "PAINT", day, 0.0517, "A")
    o2 = Alert("AL-O2", "info", "line", "WELD-1", day, 0.8738, "A")
    d1 = Alert("AL-D1", "warning", "equipment", "CONV-03", day, 55.0)
    q3 = Alert("AL-Q3", "warning", "site", "PLANT", day, {"WELD": 0.027, "PAINT": 0.0517}, "A")
    assert alert_message_ru(q1, cfg) == "Окраска: брак 5,17% (02.10.2026, смена A), норма 2%"
    assert alert_message_ru(o2, cfg) == "Сварка-1: OEE 87,38% (02.10.2026, смена A), цель 85%"
    assert alert_message_ru(d1, cfg) == (
        "Конвейер-03 (финальная): внеплановый простой 55 мин (02.10.2026), лимит 60 мин в сутки"
    )
    assert alert_message_ru(q3, cfg) == (
        "Брак вырос на всех участках (02.10.2026, смена A): Сварка 2,7%, Окраска 5,17%"
    )
