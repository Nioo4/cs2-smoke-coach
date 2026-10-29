"""Single-line structured logging with a deliberately small safe field set."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime


class SafeJsonFormatter(logging.Formatter):
    _allowed = {
        "request_id",
        "http_method",
        "path",
        "status",
        "latency_ms",
        "error_code",
        "event",
        "fields",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
        }
        for key in self._allowed:
            value = getattr(record, key, None)
            if value is not None:
                if key == "path":
                    # The middleware passes request.url.path, but keep the
                    # formatter safe if a third-party logger supplies a URL.
                    value = str(value).split("?", 1)[0].split("#", 1)[0]
                elif key == "fields":
                    if not isinstance(value, (list, tuple)) or not all(
                        isinstance(field, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", field)
                        for field in value
                    ):
                        continue
                    value = list(value)
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> logging.Logger:
    """Install the safe formatter for application and server loggers.

    Uvicorn and Alembic are started by the same container process.  They must
    not fall back to their default access/error formatters, which can include
    request metadata or exception text.  The Uvicorn ``--log-config`` file
    covers messages emitted before the application is imported; this function
    keeps the in-process loggers on the same allow-list as well.
    """

    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    for name in (
        "smokeline",
        "uvicorn",
        "uvicorn.error",
        "alembic",
        "sqlalchemy",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        # Alembic's fileConfig disables existing named loggers by default.
        # Re-enable them when restoring the application's safe formatter so a
        # migration cannot silence the bounded startup/error events.
        logger.disabled = False
        logger.propagate = False
        for existing in logger.handlers[:]:
            logger.removeHandler(existing)
        logger.addHandler(handler)

    # Access records are explicitly disabled; request completion is logged by
    # RequestContextMiddleware with the stable, query-free field set.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.disabled = True
    access_logger.propagate = False
    return logging.getLogger("smokeline")


def monotonic_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))
