import logging
import re

from fastapi import Body
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import app


class _Payload(BaseModel):
    value: int


_payload_body = Body(...)


async def _validation_endpoint(payload: _Payload = _payload_body):
    return payload


async def _secret_failure_endpoint():
    raise RuntimeError("SECRET_SENTINEL")


async def _teapot_endpoint():
    from starlette.exceptions import HTTPException

    raise HTTPException(status_code=418, detail="SECRET_SENTINEL")


app.add_api_route("/__test/validation", _validation_endpoint, methods=["POST"])
app.add_api_route("/__test/failure", _secret_failure_endpoint, methods=["GET"])
app.add_api_route("/__test/teapot", _teapot_endpoint, methods=["GET"])


def test_health_and_request_id_contract() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/healthz", headers={"X-Request-ID": "not-valid"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_valid_request_id_is_reused() -> None:
    request_id = "0123456789abcdef0123456789abcdef"
    with TestClient(app) as client:
        response = client.get("/api/v1/healthz", headers={"X-Request-ID": request_id})
    assert response.headers["X-Request-ID"] == request_id


def test_unknown_endpoint_is_json_and_not_internal_error() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/not-open-yet")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error"]["code"] == "ENDPOINT_NOT_FOUND"
    assert response.json()["error"]["message"] == "接口不存在。"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_known_endpoint_wrong_method_preserves_allow() -> None:
    with TestClient(app) as client:
        response = client.post("/api/v1/healthz")
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_cors_preflight_accepts_exact_origin_and_rejects_method() -> None:
    with TestClient(app) as client:
        allowed = client.options(
            "/api/v1/healthz",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        denied = client.options(
            "/api/v1/healthz",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "DELETE",
            },
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert denied.status_code == 400
    assert denied.headers["Cache-Control"] == "no-store"
    assert denied.json()["error"]["code"] == "CORS_REQUEST_DENIED"
    assert denied.json()["error"]["request_id"] == denied.headers["X-Request-ID"]


def test_validation_and_unhandled_errors_are_safe_json() -> None:
    completion_records: list[logging.LogRecord] = []

    class _CollectHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            completion_records.append(record)

    logger = logging.getLogger("smokeline")
    handler = _CollectHandler()
    logger.addHandler(handler)
    with TestClient(app, raise_server_exceptions=False) as client:
        try:
            invalid_json = client.post(
                "/__test/validation",
                content="{",
                headers={"Content-Type": "application/json"},
            )
            invalid_field = client.post("/__test/validation", json={"value": "bad"})
            failure = client.get("/__test/failure")
            teapot = client.get("/__test/teapot")
        finally:
            logger.removeHandler(handler)

    assert invalid_json.status_code == 400
    assert invalid_json.json()["error"]["code"] == "INVALID_JSON"
    assert invalid_json.headers["Cache-Control"] == "no-store"
    assert invalid_json.headers["X-Request-ID"] == invalid_json.json()["error"]["request_id"]
    assert invalid_field.status_code == 422
    assert invalid_field.headers["Cache-Control"] == "no-store"
    assert invalid_field.headers["X-Request-ID"] == invalid_field.json()["error"]["request_id"]
    assert invalid_field.json()["error"]["details"] == {
        "fields": [{"path": "value", "reason": "int_parsing"}]
    }
    assert failure.status_code == 500
    assert "SECRET_SENTINEL" not in failure.text
    assert failure.headers["Cache-Control"] == "no-store"
    assert failure.headers["X-Request-ID"] == failure.json()["error"]["request_id"]
    assert any(
        record.event == "request_completed"
        and record.status == 500
        and record.error_code == "INTERNAL_ERROR"
        for record in completion_records
    )
    assert teapot.status_code == 500
    assert teapot.headers["Cache-Control"] == "no-store"
    assert teapot.headers["X-Request-ID"] == teapot.json()["error"]["request_id"]
    assert teapot.json()["error"]["code"] == "INTERNAL_ERROR"


def test_ready_reports_database_failure(monkeypatch) -> None:
    async def unavailable(_engine=None):
        return False

    monkeypatch.setattr("app.main.check_database", unavailable)
    with TestClient(app) as client:
        response = client.get("/api/v1/readyz")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
