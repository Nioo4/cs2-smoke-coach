"""Real isolated teaching rows, Node WS commands and private public-rule resolution."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import httpx
import pytest
import test_tactics_collaboration as tcp
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config
from app.db.models import Lineup, LineupStatus, Origin, Target, TutorialVideo, VideoStatus
from app.schemas.public import LineupDetail
from app.scripts.run_tactics_accounts_selftest import create_app
from app.services.tactics_references import resolve_reference

# Reuse the actual TCP fixture while overriding its explicitly isolated database fixture here.
ORIGIN, ROOT = tcp.ORIGIN, tcp.ROOT
event_loop_policy, live = tcp.event_loop_policy, tcp.live

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_REFERENCES_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.database == "tactics_references_selftest"
    assert parsed.port != 55433
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        config.get_settings = previous
    return raw


@pytest.fixture
async def catalog(live):
    _, _, factory, _ = live
    async with factory() as db, db.begin():
        suffix = uuid.uuid4().hex[:16]
        target = Target(slug=f"refs-target-{suffix}", name_zh="隔离教学目标", map_x=50, map_y=50)
        origin = Origin(slug=f"refs-origin-{suffix}", name_zh="隔离教学站位", map_x=25, map_y=25)
        db.add_all([target, origin])
        await db.flush()
        identities = {}
        for name, status, default in [("visible", LineupStatus.published, True),
                                       ("variant", LineupStatus.published, False),
                                       ("hidden", LineupStatus.draft, False)]:
            lineup = Lineup(target_id=target.id, origin_id=origin.id, status=status,
                            is_default=default,
                            setup_text="隐藏教学秘密" if name == "hidden" else "隔离站位",
                            aim_text="隔离瞄点", throw_text="隔离投掷", result_text="隔离结果")
            db.add(lineup)
            await db.flush()
            storage = uuid.uuid4()
            db.add(TutorialVideo(lineup_id=lineup.id, status=VideoStatus.ready,
                                 storage_key=storage, relative_path=f"{storage}/fixture.mp4",
                                 poster_relative_path=f"{storage}/fixture.webp", duration_ms=1000,
                                 size_bytes=1024, checksum_sha256="a" * 64,
                                 rights_note="Isolated test metadata; no copied media"))
            identities[name] = str(lineup.id)
        identities["target"] = str(target.id)
        identities["target_slug"] = target.slug
    return identities


async def test_references_real_websocket_visibility_preservation_and_copy(
    live, catalog, database_url
):
    backend, seed, _, _ = live
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-references-smoke.mjs"], cwd=ROOT,
        env={**os.environ, "FORMAL_TEST_BACKEND": backend,
             "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
             "FORMAL_TEST_ORIGIN": ORIGIN,
             "REFERENCES_TEST_DATABASE_URL": database_url.replace(
                 "postgresql+psycopg://", "postgresql://"),
             "REFERENCES_TEST_CATALOG": json.dumps(catalog)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=30)
        assert process.returncode == 0, stderr.decode()
        assert b"FORMAL_REFERENCES_HTTP_WS_PG_PASS" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)


async def test_resolver_reuses_unicode_whitespace_and_video_visibility_rules(live, catalog):
    _, _, factory, _ = live
    async with factory() as db:
        visible = await resolve_reference(db, uuid.UUID(catalog["visible"]))
        LineupDetail.model_validate(visible["lineup"])
        assert visible["lineup"]["id"] == catalog["visible"]
        unknown = uuid.uuid4()
        assert await resolve_reference(db, unknown) == {
            "id": str(unknown), "available": False, "lineup": None
        }
        assert await resolve_reference(db, uuid.UUID(catalog["hidden"])) == {
            "id": catalog["hidden"], "available": False, "lineup": None
        }
    async with factory() as db, db.begin():
        lineup = await db.get(Lineup, uuid.UUID(catalog["visible"]))
        lineup.setup_text = "\n\t"
    async with factory() as db:
        assert (await resolve_reference(db, uuid.UUID(catalog["visible"])))["available"] is False


async def test_isolated_harness_catalog_reads_use_its_own_database(
    live, catalog, database_url, monkeypatch
):
    monkeypatch.setenv("TACTICS_ACCOUNTS_TEST_DATABASE_URL", database_url)
    monkeypatch.setenv("TACTICS_ACCOUNTS_TEST_ORIGIN", "http://127.0.0.1:4189")
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            summary = await client.get("/api/v1/maps/mirage")
            assert summary.status_code == 200
            assert catalog["target_slug"] in {row["slug"] for row in summary.json()["targets"]}
            choices = await client.get(f"/api/v1/targets/{catalog['target_slug']}/lineups")
            assert choices.status_code == 200
            assert choices.json()["origins"][0]["lineup"]["id"] == catalog["visible"]
            detail = await client.get(f"/api/v1/lineups/{catalog['visible']}")
            assert detail.status_code == 200
            LineupDetail.model_validate(detail.json())
            assert (await client.get(f"/api/v1/lineups/{catalog['hidden']}")).status_code == 404
            assert (await client.post("/api/v1/search", json={})).status_code == 404
