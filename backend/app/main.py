"""SmokeLine API application and shared request/error plumbing."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.admin import router as admin_router
from app.api.public import router as public_router
from app.core.config import get_settings
from app.core.errors import AppError, error_body, install_error_handlers
from app.core.logging import configure_logging, monotonic_ms
from app.core.security import normalize_request_id
from app.db.session import check_database, get_engine
from app.services.metrics import record_public_request

settings = get_settings()
logger = configure_logging()


def _is_public_business_request(request: Request) -> bool:
    """Identify public business routes without counting health probes."""

    if request.method not in {"GET", "POST"}:
        return False
    path = request.url.path
    return (
        path in {"/api/v1/maps/mirage", "/api/v1/search", "/api/v1/transcriptions"}
        or (path.startswith("/api/v1/targets/") and path.endswith("/lineups"))
        or path.startswith("/api/v1/lineups/")
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        request.state.request_id = normalize_request_id(request.headers.get("x-request-id"))
        try:
            response = await call_next(request)
        except Exception:
            # ServerErrorMiddleware normally handles this path outside the
            # request context.  Generate the safe response here so failures
            # carry the same request id, cache policy, and completion log as
            # successful and framework-handled responses.
            response = JSONResponse(
                status_code=500,
                content=error_body(
                    request,
                    "INTERNAL_ERROR",
                    "服务暂时异常，请凭请求编号重试。",
                ),
                headers={"Cache-Control": "no-store"},
            )
        if _is_public_business_request(request):
            event = getattr(request.state, "public_event", {})
            try:
                await record_public_request(
                    request.state.request_id,
                    input_mode=getattr(
                        request.state,
                        "public_input_mode",
                        "map" if request.method == "GET" else "text",
                    ),
                    response_type=event.get("response_type"),
                    target_slug=event.get("target_slug"),
                    origin_slug=event.get("origin_slug"),
                    latency_ms=monotonic_ms(started),
                    error_code=getattr(request.state, "error_code", None)
                    or event.get("error_code"),
                )
            except Exception:
                # Metrics are deliberately best-effort and must never replace
                # the already-formed business response.
                pass
        if (
            settings.app_env in {"development", "test"}
            and request.method == "OPTIONS"
            and request.headers.get("origin")
            and request.headers.get("access-control-request-method")
            and response.status_code == 400
        ):
            response = JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "CORS_REQUEST_DENIED",
                        "message": "开发环境不允许该跨域请求。",
                        "request_id": request.state.request_id,
                    }
                },
                headers={"Cache-Control": "no-store"},
            )
            request.state.error_code = "CORS_REQUEST_DENIED"
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/v1"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        logger.info(
            "http",
            extra={
                "event": "request_completed",
                "request_id": request.state.request_id,
                "http_method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": monotonic_ms(started),
                "error_code": getattr(request.state, "error_code", None),
            },
        )
        return response


app = FastAPI(
    title="SmokeLine API",
    version="0.1.0",
    docs_url=None if settings.app_env == "production" else "/docs",
    openapi_url=None if settings.app_env == "production" else "/openapi.json",
)
install_error_handlers(app)

if settings.app_env != "production":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.add_middleware(RequestContextMiddleware)
app.include_router(public_router)
app.include_router(admin_router)


@app.get("/api/v1/healthz", include_in_schema=True)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/readyz", include_in_schema=True)
async def readyz() -> dict[str, str]:
    if not await check_database(get_engine()):
        raise AppError(503, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试。")
    return {"status": "ready"}


@app.get("/", include_in_schema=False)
async def backend_root() -> JSONResponse:
    return JSONResponse({"service": "smokeline", "status": "ok"})
