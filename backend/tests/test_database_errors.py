from __future__ import annotations

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.errors import install_error_handlers


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (
            OperationalError("hidden-sql", {}, psycopg.OperationalError("private-marker")),
            503,
            "DATABASE_UNAVAILABLE",
        ),
        (
            OperationalError("hidden-sql", {}, psycopg.errors.ConnectionFailure("private-marker")),
            503,
            "DATABASE_UNAVAILABLE",
        ),
        (
            OperationalError("hidden-sql", {}, psycopg.errors.AdminShutdown("private-marker")),
            503,
            "DATABASE_UNAVAILABLE",
        ),
        (
            OperationalError("hidden-sql", {}, psycopg.errors.TooManyConnections("private-marker")),
            503,
            "DATABASE_UNAVAILABLE",
        ),
        (PoolTimeoutError("private-marker"), 503, "DATABASE_UNAVAILABLE"),
        (
            OperationalError("hidden-sql", {}, psycopg.errors.LockNotAvailable("private-marker")),
            500,
            "INTERNAL_ERROR",
        ),
        (
            IntegrityError("hidden-sql", {}, psycopg.errors.UniqueViolation("private-marker")),
            500,
            "INTERNAL_ERROR",
        ),
    ],
)
def test_database_failure_categories_do_not_leak_details(
    failure: Exception, expected_status: int, expected_code: str
) -> None:
    application = FastAPI()
    install_error_handlers(application)

    @application.get("/failure")
    async def failing(request: Request):
        request.state.request_id = "c" * 32
        raise failure

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/failure")
    assert response.status_code == expected_status
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error"]["code"] == expected_code
    assert response.json()["error"]["request_id"] == "c" * 32
    assert "private-marker" not in response.text
    assert "hidden-sql" not in response.text
