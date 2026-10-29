"""Run the migration gate and then replace the process with Uvicorn."""

from __future__ import annotations

import asyncio
import os
import sys

from alembic.config import Config

from alembic import command
from app.core.config import ConfigError, get_settings
from app.core.logging import configure_logging


def _log_failure(event: str, error_code: str, fields: tuple[str, ...] = ()) -> None:
    # Re-install the formatter because Alembic's fileConfig may have replaced
    # handlers while loading the migration environment.
    logger = configure_logging()
    logger.error(
        event,
        extra={"event": event, "error_code": error_code, "fields": list(fields)},
    )


async def _run_media_reconcile() -> None:
    from app.scripts.reconcile_media import reconcile_media
    from app.services.media import media_lock

    async with media_lock(timeout_seconds=120):
        await reconcile_media()


def main() -> int:
    logger = configure_logging()
    try:
        settings = get_settings()
    except ConfigError as exc:
        logger.error(
            "configuration",
            extra={
                "event": "configuration_rejected",
                "error_code": "CONFIGURATION_INVALID",
                "fields": list(exc.fields),
            },
        )
        return 78

    try:
        alembic_config = Config("alembic.ini")
        # env.py performs the same escaping before it hands the URL to
        # ConfigParser/SQLAlchemy; setting it here keeps offline invocation
        # consistent with the normal container path.
        alembic_config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
        command.upgrade(alembic_config, "head")
    except ConfigError as exc:
        _log_failure("configuration", "CONFIGURATION_INVALID", exc.fields)
        return 78
    except Exception:
        _log_failure("migration", "MIGRATION_FAILED")
        return 1

    try:
        asyncio.run(_run_media_reconcile())
    except Exception:
        _log_failure("media_reconcile", "MEDIA_RECONCILE_FAILED")
        return 1

    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--no-access-log",
            "--log-config",
            "/opt/smokeline/backend/uvicorn-log-config.json",
        ],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
