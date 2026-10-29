"""Stable JSON errors shared by every API handler."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def database_is_unavailable(exc: DBAPIError | PoolTimeoutError) -> bool:
    """Separate connection/pool failures from SQL defects and constraint errors."""

    if isinstance(exc, PoolTimeoutError):
        return True
    code = getattr(exc.orig, "sqlstate", None)
    return bool(
        exc.connection_invalidated
        or (isinstance(exc, OperationalError) and code is None)
        or (isinstance(code, str) and code.startswith("08"))
        or code in {"53300", "57P01", "57P02", "57P03"}
    )


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "00000000000000000000000000000000")


def error_body(
    request: Request,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request.state.error_code = code
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id(request),
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


def _validation_details(exc: RequestValidationError) -> dict[str, list[dict[str, str]]]:
    fields: list[dict[str, str]] = []
    for item in exc.errors():
        location = list(item.get("loc", ()))
        if location and location[0] in {"body", "query", "path", "header"}:
            source = location.pop(0)
            # A discriminated search union adds its branch tag before the
            # actual field path, e.g. body,selection,selection,field.
            if source == "body" and location and location[0] in {"query", "selection"}:
                location.pop(0)
        path = ".".join(str(part) for part in location)
        fields.append(
            {
                "path": path or "request",
                "reason": str(item.get("type", "invalid")).replace("-", "_"),
            }
        )
    return {"fields": fields}


def install_error_handlers(app) -> None:
    @app.exception_handler(OperationalError)
    @app.exception_handler(PoolTimeoutError)
    async def database_error_handler(
        request: Request, exc: OperationalError | PoolTimeoutError
    ) -> JSONResponse:
        unavailable = database_is_unavailable(exc)
        return JSONResponse(
            status_code=503 if unavailable else 500,
            content=error_body(
                request,
                "DATABASE_UNAVAILABLE" if unavailable else "INTERNAL_ERROR",
                "数据库暂时不可用，请稍后重试。"
                if unavailable
                else "服务暂时异常，请凭请求编号重试。",
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request, exc.code, exc.message, exc.details),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        is_json_error = any(item.get("type") == "json_invalid" for item in exc.errors())
        if is_json_error:
            status_code, code, message, details = 400, "INVALID_JSON", "请求 JSON 无法解析。", None
        else:
            status_code, code, message, details = (
                422,
                "VALIDATION_ERROR",
                "请求参数不合法。",
                _validation_details(exc),
            )
        return JSONResponse(
            status_code=status_code,
            content=error_body(request, code, message, details),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code, message = "ENDPOINT_NOT_FOUND", "接口不存在。"
            status_code = 404
        elif exc.status_code == 405:
            code, message = "METHOD_NOT_ALLOWED", "该接口不支持此请求方法。"
            status_code = 405
        else:
            code, message = "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。"
            status_code = 500
        headers = {"Cache-Control": "no-store"}
        allow = (exc.headers or {}).get("Allow") or (exc.headers or {}).get("allow")
        if exc.status_code == 405 and allow:
            headers["Allow"] = allow
        return JSONResponse(
            status_code=status_code,
            content=error_body(request, code, message),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body(request, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。"),
            headers={"Cache-Control": "no-store"},
        )
