"""Vertical real Node HTTP seed -> FastAPI -> dedicated PostgreSQL document tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.api import tactics, tactics_auth
from app.core import config
from app.core.errors import install_error_handlers
from app.db.tactics_models import Tactic, TacticDocument, TacticVersion
from app.services import tactics_accounts as accounts
from app.services import tactics_documents as documents

pytestmark = pytest.mark.integration
ORIGIN = "http://notebooks.test"
KEY = "local-seed-integration-key-not-secret-2026"


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else (
        asyncio.DefaultEventLoopPolicy()
    )


@pytest.fixture(scope="module")
def isolated_database_url():
    raw = os.environ.get("TACTICS_NOTEBOOKS_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.database in {
        "tactics_notebooks_selftest", "tactics_formal_selftest"
    }
    assert parsed.port != 55433
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        target = "head" if parsed.database == "tactics_formal_selftest" else (
            "0007_tactics_documents"
        )
        command.upgrade(Config("alembic.ini"), target)
    finally:
        config.get_settings = previous
    return raw


@pytest.fixture(scope="module")
def seed_url():
    root = Path(__file__).resolve().parents[2] / "collaboration"
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-seed-server.mjs"], cwd=root,
        env={**os.environ, "COLLAB_INTERNAL_KEY": KEY, "TACTICS_FORMAL_PORT": "0"},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        line = process.stdout.readline()
        assert line.startswith("FORMAL_SEED_LISTENING "), "isolated Node seed service did not start"
        address = json.loads(line.split(" ", 1)[1])
        yield f"http://127.0.0.1:{address['port']}"
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture
async def fixture(isolated_database_url, seed_url, monkeypatch):
    engine = create_async_engine(isolated_database_url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = config.Settings(_env_file=None, app_env="test", database_url=isolated_database_url,
                               tactics_enabled=True, public_base_url=ORIGIN,
                               collab_internal_url=seed_url, collab_internal_key=KEY)
    monkeypatch.setattr(tactics_auth, "get_session_factory", lambda: factory)
    monkeypatch.setattr(accounts, "get_session_factory", lambda: factory)
    monkeypatch.setattr(tactics_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(documents, "get_settings", lambda: settings)
    accounts._RATE_WINDOWS.clear()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(tactics_auth.router)
    app.include_router(tactics.router)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        registration = await client.post("/api/v1/auth/register", headers={"origin": ORIGIN},
                                         json={"username": "n_" + uuid.uuid4().hex[:16],
                                               "display_name": "目录测试",
                                               "password": "notebook-test-password-2026"})
        assert registration.status_code == 201
        headers = {"origin": ORIGIN, "x-csrf-token": registration.json()["csrf_token"]}
        notebook_id = (await client.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        yield client, notebook_id, headers, factory, settings, transport
    await engine.dispose()


async def create(client, notebook_id, headers, title="创建真实中文战术", key=None):
    return await client.post(f"/api/v1/notebooks/{notebook_id}/tactics",
                             headers={**headers, "idempotency-key": key or str(uuid.uuid4())},
                             json={"title": title, "side": "CT"})


async def test_create_read_list_and_idempotency_use_real_node_seed(fixture):
    client, notebook_id, headers, factory, settings, _ = fixture
    key = str(uuid.uuid4())
    created = await create(client, notebook_id, headers, key=key)
    assert created.status_code == 201, created.text
    data = created.json()
    tactic_id = data["tactic"]["id"]
    assert data["projection"]["structure"]["schema_version"] == 2
    assert data["projection"]["fields"]["tactic.title"] == "创建真实中文战术"
    assert data["tactic"]["revision"] == 0
    assert (await client.get(f"/api/v1/tactics/{tactic_id}")).json() == data
    async with factory() as db:
        document = await db.get(TacticDocument, uuid.UUID(tactic_id))
        assert document.yjs_state and document.state_digest
        assert document.projection == data["projection"]
    settings.collab_internal_url = "http://127.0.0.1:1"
    repeated = await create(client, notebook_id, headers, key=key)
    assert repeated.status_code == 201 and repeated.json()["tactic"]["id"] == tactic_id
    conflict = await create(client, notebook_id, headers, title="不同内容", key=key)
    assert conflict.status_code == 409
    unavailable = await create(client, notebook_id, headers)
    assert unavailable.status_code == 503
    listed = (await client.get(f"/api/v1/notebooks/{notebook_id}/tactics")).json()
    assert len(listed["items"]) == 1 and listed["next_cursor"] is None


async def test_archive_restore_cas_epoch_and_snapshot(fixture):
    client, notebook_id, headers, factory, _, _ = fixture
    data = (await create(client, notebook_id, headers)).json()
    tactic_id = data["tactic"]["id"]
    path = f"/api/v1/tactics/{tactic_id}"
    key = str(uuid.uuid4())
    write = {**headers, "idempotency-key": key}
    stale = await client.post(path + "/archive", headers=write, json={"expected_revision": 8})
    assert stale.status_code == 409
    archived = await client.post(path + "/archive", headers=write, json={"expected_revision": 0})
    assert archived.status_code == 200, archived.text
    assert (await client.post(path + "/archive", headers=write,
                              json={"expected_revision": 0})).json() == archived.json()
    assert (await client.get(path)).status_code == 404
    saved = (await client.get(path + "/archived")).json()
    assert saved["epoch"] != data["epoch"] and saved["projection"] == data["projection"]
    assert saved["permissions"]["edit"] is False
    trash = (await client.get(f"/api/v1/notebooks/{notebook_id}/tactics?deleted=true")).json()
    assert len(trash["items"]) == 1
    restored = await client.post(path + "/restore", headers={
        **headers, "idempotency-key": str(uuid.uuid4())}, json={})
    assert restored.status_code == 200 and restored.json()["revision"] == 2
    latest = (await client.get(path)).json()
    assert latest["epoch"] not in {data["epoch"], saved["epoch"]}
    async with factory() as db:
        version = await db.scalar(sa.select(TacticVersion).where(
            TacticVersion.tactic_id == uuid.UUID(tactic_id)
        ))
        assert version.source_revision == 0 and version.projection == data["projection"]


async def test_directory_isolation_strictness_and_pagination(fixture):
    client, notebook_id, headers, _, _, transport = fixture
    first = await create(client, notebook_id, headers, title="100% tactic")
    tactic_id = first.json()["tactic"]["id"]
    await create(client, notebook_id, headers, title="100x tactic")
    path = f"/api/v1/notebooks/{notebook_id}/tactics"
    filtered = (await client.get(path, params={"q": "%"})).json()
    assert [item["title"] for item in filtered["items"]] == ["100% tactic"]
    page = (await client.get(path, params={"limit": 1})).json()
    following = (await client.get(path, params={"limit": 1, "cursor": page["next_cursor"]})).json()
    assert len(following["items"]) == 1 and following["items"][0]["id"] != page["items"][0]["id"]
    assert (await client.get(path + "?limit=1&limit=2")).status_code == 422
    assert (await client.get(path + "?owner=forged")).status_code == 422
    missing_key = await client.post(path, headers=headers, json={"title": "x", "side": "T"})
    assert missing_key.status_code == 422
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        result = await other.post("/api/v1/auth/register", headers={"origin": ORIGIN}, json={
            "username": "other_" + uuid.uuid4().hex[:12], "display_name": "其他用户",
            "password": "notebook-test-password-2026",
        })
        other_headers = {"origin": ORIGIN, "x-csrf-token": result.json()["csrf_token"]}
        assert (await other.get(path)).status_code == 404
        assert (await other.get(f"/api/v1/tactics/{tactic_id}")).status_code == 404
        assert (await create(other, notebook_id, other_headers)).status_code == 404


async def test_concurrent_same_key_creates_one_and_restore_respects_expiry(fixture):
    client, notebook_id, headers, factory, _, _ = fixture
    key = str(uuid.uuid4())
    results = await asyncio.gather(*(
        create(client, notebook_id, headers, key=key) for _ in range(2)
    ))
    assert all(result.status_code == 201 for result in results)
    assert len({result.json()["tactic"]["id"] for result in results}) == 1
    tactic_id = results[0].json()["tactic"]["id"]
    async with factory() as db, db.begin():
        tactic = await db.get(Tactic, uuid.UUID(tactic_id))
        tactic.status, tactic.archived_at = "archived", accounts.now() - timedelta(days=31)
    result = await client.post(f"/api/v1/tactics/{tactic_id}/restore", headers={
        **headers, "idempotency-key": str(uuid.uuid4())}, json={})
    assert result.status_code == 404


async def test_real_document_insert_failure_rolls_back_entire_create(fixture):
    client, notebook_id, headers, factory, _, _ = fixture
    async with factory() as db, db.begin():
        await db.execute(sa.text("""CREATE OR REPLACE FUNCTION notebook_test_reject_document()
          RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test failure'; END $$"""))
        await db.execute(sa.text("""CREATE TRIGGER notebook_test_document_failure
          BEFORE INSERT ON tactic_documents FOR EACH ROW
          EXECUTE FUNCTION notebook_test_reject_document()"""))
    try:
        result = await create(client, notebook_id, headers)
        assert result.status_code == 500
        listed = (await client.get(f"/api/v1/notebooks/{notebook_id}/tactics")).json()
        assert listed["items"] == []
    finally:
        async with factory() as db, db.begin():
            await db.execute(sa.text(
                "DROP TRIGGER notebook_test_document_failure ON tactic_documents"
            ))
            await db.execute(sa.text("DROP FUNCTION notebook_test_reject_document()"))


async def test_quota_serializes_racing_creates_and_restore(fixture):
    client, notebook_id, headers, _, _, _ = fixture
    first = None
    for index in range(99):
        result = await create(client, notebook_id, headers, title=f"配额{index}")
        assert result.status_code == 201
        first = first or result.json()["tactic"]["id"]
    final = await asyncio.gather(*(create(client, notebook_id, headers) for _ in range(2)))
    assert sorted(result.status_code for result in final) == [201, 409]
    path = f"/api/v1/tactics/{first}"
    assert (await client.post(path + "/archive", headers={
        **headers, "idempotency-key": str(uuid.uuid4())},
        json={"expected_revision": 0})).status_code == 200
    assert (await create(client, notebook_id, headers)).status_code == 201
    assert (await client.post(path + "/restore", headers={
        **headers, "idempotency-key": str(uuid.uuid4())}, json={})).status_code == 409


async def test_real_seed_response_digests_and_projection_tampering_are_rejected(fixture):
    _, _, _, _, settings, _ = fixture
    identity = uuid.uuid4()
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await client.post(settings.collab_internal_url + "/internal/seed-stage-document",
                                   headers={"internal-key": KEY},
                                   json={"document_id": str(identity),
                                         "title": "真实种子", "side": "CT"})
    packet = result.json()
    assert documents.validate_seed_packet(packet, identity, "真实种子", "CT")[0]
    packet["projection"]["fields"]["tactic.title"] = "篡改目录"
    with pytest.raises(ValueError):
        documents.validate_seed_packet(packet, identity, "真实种子", "CT")
    packet["projection_json"] = documents.json_bytes(packet["projection"]).decode()
    packet["projection_sha256"] = hashlib.sha256(packet["projection_json"].encode()).hexdigest()
    with pytest.raises(ValueError):
        documents.validate_seed_packet(packet, identity, "真实种子", "CT")
