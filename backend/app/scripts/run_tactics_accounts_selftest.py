"""Loopback-only account UI harness with an explicitly named isolated database, no .env."""

from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import (
    features,
    public,
    tactics,
    tactics_auth,
    tactics_collaboration,
    tactics_shares,
    tactics_teams,
)
from app.core.config import Settings
from app.core.errors import install_error_handlers
from app.services import local_search, tactics_accounts, tactics_documents
from app.services.tactics_history import history_worker, stop_worker


def create_app() -> FastAPI:
    raw = os.environ.get("TACTICS_ACCOUNTS_TEST_DATABASE_URL", "")
    target = make_url(raw)
    if target.host != "127.0.0.1" or target.database not in {
        "tactics_accounts_selftest", "tactics_notebooks_selftest", "tactics_formal_selftest",
        "tactics_references_selftest", "tactics_teams_selftest", "tactics_shares_selftest"
    } or (
        target.port == 55433
    ):
        raise RuntimeError("Use only the explicitly isolated loopback account test database")
    origin = os.environ.get("TACTICS_ACCOUNTS_TEST_ORIGIN", "http://127.0.0.1:4184")
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or (
        parsed.query or parsed.fragment or parsed.username or parsed.password
    ):
        raise RuntimeError("The test origin must be a loopback HTTP origin without path")
    settings = Settings(_env_file=None, app_env="test", tactics_enabled=True,
                        database_url=raw, public_base_url=origin, temp_root=tempfile.gettempdir())
    engine = create_async_engine(raw, hide_parameters=True, pool_size=5, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tactics_auth.get_settings = lambda: settings
    tactics_auth.get_session_factory = lambda: factory
    tactics_accounts.get_session_factory = lambda: factory
    local_search.get_session_factory = lambda: factory
    tactics_documents.get_settings = lambda: settings
    features.get_settings = lambda: settings
    tactics_collaboration.get_settings = lambda: settings

    @asynccontextmanager
    async def lifespan(_app):
        worker = asyncio.create_task(history_worker(settings))
        try:
            yield
        finally:
            await stop_worker(worker)
            await engine.dispose()

    app = FastAPI(title="SmokeLine isolated T1-A account self-test", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(tactics_auth.router)
    app.include_router(tactics.router)
    app.include_router(features.router)
    app.include_router(tactics_collaboration.router)
    app.include_router(tactics_collaboration.internal_router)
    app.include_router(tactics_teams.router)
    app.include_router(tactics_shares.router)
    app.include_router(tactics_shares.public_router)
    # Reuse only catalog GET routes; search/audio and their providers are not mounted.
    catalog_paths = {
        "/api/v1/maps/mirage", "/api/v1/targets/{target_slug}/lineups",
        "/api/v1/lineups/{lineup_id}",
    }
    app.router.routes.extend(route for route in public.router.routes
                             if route.path in catalog_paths and route.methods == {"GET"})
    return app


def main() -> None:
    port = int(os.environ.get("TACTICS_ACCOUNTS_TEST_PORT", "4184"))
    if not 1024 <= port <= 65535:
        raise RuntimeError("Invalid loopback test port")
    uvicorn.run(
        create_app(), host="127.0.0.1", port=port, access_log=False,
        loop="asyncio:SelectorEventLoop",
    )


if __name__ == "__main__":
    main()
