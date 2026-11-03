"""Real TCP FastAPI, Node WebSockets and a dedicated PostgreSQL collaboration database."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.api import (
    features,
    tactics,
    tactics_auth,
    tactics_collaboration,
    tactics_shares,
    tactics_teams,
)
from app.core import config
from app.core.errors import install_error_handlers
from app.schemas.tactics_document import validate_projection
from app.services import tactics_accounts as accounts
from app.services import tactics_documents as documents

pytestmark = pytest.mark.integration
ORIGIN = "http://formal.test"
KEY = "local-formal-collaboration-test-key-2026"
ROOT = Path(__file__).resolve().parents[2] / "collaboration"


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else (
        asyncio.DefaultEventLoopPolicy()
    )


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_FORMAL_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.database == "tactics_formal_selftest"
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
async def live(database_url, monkeypatch):
    engine = create_async_engine(database_url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = config.Settings(_env_file=None, app_env="test", database_url=database_url,
                               tactics_enabled=True, public_base_url=ORIGIN,
                               collab_internal_key=KEY)
    monkeypatch.setattr(tactics_auth, "get_session_factory", lambda: factory)
    monkeypatch.setattr(accounts, "get_session_factory", lambda: factory)
    for module in [tactics_auth, tactics_collaboration, documents, features]:
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    accounts._RATE_WINDOWS.clear()
    app = FastAPI()
    install_error_handlers(app)
    for router in [tactics_auth.router, tactics.router, tactics_collaboration.router,
                   tactics_collaboration.internal_router, features.router, tactics_teams.router,
                   tactics_shares.router, tactics_shares.public_router]:
        app.include_router(router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app=app, log_level="critical", access_log=False,
                                           lifespan="off", loop="asyncio:SelectorEventLoop"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    backend = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-seed-server.mjs"], cwd=ROOT,
        env={**os.environ, "COLLAB_INTERNAL_KEY": KEY, "TACTICS_FORMAL_PORT": "0",
             "TACTICS_FORMAL_HOST": "127.0.0.1", "COLLAB_BACKEND_URL": backend,
             "PUBLIC_BASE_URL": ORIGIN}, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=5)
        assert line.startswith("FORMAL_SEED_LISTENING "), "formal Node did not start"
        address = json.loads(line.split(" ", 1)[1])
        seed = f"http://127.0.0.1:{address['port']}"
        settings.collab_internal_url = seed
        yield backend, seed, factory, settings
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, 5)
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        sock.close()
        await engine.dispose()


async def test_real_http_ws_pg_operations_conflicts_recovery_and_revocation(live, database_url):
    backend, seed, _, _ = live
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-protocol-smoke.mjs"], cwd=ROOT,
        env={**os.environ, "FORMAL_TEST_BACKEND": backend,
             "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
             "FORMAL_TEST_ORIGIN": ORIGIN,
             "FORMAL_TEST_INTERNAL_KEY": KEY,
             "FORMAL_TEST_DATABASE_URL": database_url.replace("postgresql+psycopg://", "postgresql://")},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=45)
        assert process.returncode == 0, stderr.decode()
        assert b"FORMAL_PROTOCOL_REAL_HTTP_WS_PG_PASS" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)


async def test_single_use_ticket_internal_key_and_disabled_routes(live):
    backend, _, _, settings = live
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as client:
        result = await client.post("/api/v1/auth/register", headers={"origin": ORIGIN}, json={
            "username": "ticket_" + uuid.uuid4().hex[:12], "display_name": "票据测试",
            "password": "formal-ticket-test-password-2026",
        })
        csrf = result.json()["csrf_token"]
        headers = {"origin": ORIGIN, "x-csrf-token": csrf}
        notebook = (await client.get("/api/v1/notebooks/personal")).json()["notebook"]
        created = await client.post(f"/api/v1/notebooks/{notebook['id']}/tactics",
                                    headers={**headers, "idempotency-key": str(uuid.uuid4())},
                                    json={"title": "票据边界", "side": "T"})
        identity = created.json()["tactic"]["id"]
        ticket = (await client.post(f"/api/v1/tactics/{identity}/collaboration-ticket",
                                    headers=headers, json={})).json()["ticket"]
        body = {"ticket": ticket, "connection_id": str(uuid.uuid4()),
                "node_instance_id": str(uuid.uuid4())}
        path = "/api/v1/internal/tactics/tickets/consume"
        assert (await client.post(path, json=body)).status_code == 401
        grant = await client.post(path, headers={"internal-key": KEY}, json=body)
        assert grant.status_code == 201
        duplicate = await client.post(path, headers={"internal-key": KEY}, json=body)
        assert duplicate.status_code == 401
        settings.tactics_enabled = False
        assert (await client.get("/api/v1/features")).json() == {"tactics_enabled": False}
        assert (await client.post(f"/api/v1/tactics/{identity}/collaboration-ticket",
                                  headers=headers, json={})).status_code == 404
        assert (await client.post(f"/api/v1/internal/tactics/{identity}/load",
                                  headers={"internal-key": KEY},
                                  json={"grant_id": grant.json()["grant_id"]})).status_code == 404


async def test_existing_websocket_closes_when_feature_is_disabled(live):
    backend, seed, _, settings = live
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as client:
        data = (await client.post("/api/v1/auth/register", headers={"origin": ORIGIN}, json={
            "username": "off_" + uuid.uuid4().hex[:12], "display_name": "关闭开关",
            "password": "formal-disable-test-password-2026",
        })).json()
        headers = {"origin": ORIGIN, "x-csrf-token": data["csrf_token"]}
        notebook = (await client.get("/api/v1/notebooks/personal")).json()["notebook"]
        created = (await client.post(f"/api/v1/notebooks/{notebook['id']}/tactics",
                                     headers={**headers, "idempotency-key": str(uuid.uuid4())},
                                     json={"title": "关停现有连接", "side": "T"})).json()
        ticket = (await client.post(
            f"/api/v1/tactics/{created['tactic']['id']}/collaboration-ticket",
            headers=headers, json={}
        )).json()["ticket"]
        process = subprocess.Popen(
            [shutil.which("node"), "scripts/formal-wait-close-smoke.mjs"], cwd=ROOT,
            env={**os.environ, "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
                 "FORMAL_TEST_ORIGIN": ORIGIN, "FORMAL_TEST_TICKET": ticket},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            ready = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=5)
            assert ready == b"CONNECTED\n"
            settings.tactics_enabled = False
            stdout, stderr = await asyncio.to_thread(process.communicate, timeout=5)
            assert process.returncode == 0 and b"CLOSED 1013" in stdout, stderr.decode()
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 5)


async def test_node_python_projection_goldens(live):
    _, seed, _, _ = live
    async with httpx.AsyncClient(trust_env=False) as client:
        packet = (await client.post(seed + "/internal/seed-stage-document",
                                    headers={"internal-key": KEY}, json={
                                        "document_id": str(uuid.uuid4()),
                                        "title": "中文😀金样本", "side": "T"})).json()
    projection = packet["projection"]
    cases = [projection]
    upper = copy.deepcopy(projection)
    upper["structure"]["roles"][0]["id"] = upper["structure"]["roles"][0]["id"].upper()
    cases.append(upper)
    invalid_version = copy.deepcopy(projection)
    invalid_version["structure"]["stages"][0]["version"] = True
    cases.append(invalid_version)
    move = copy.deepcopy(projection)
    role_id = move["structure"]["roles"][0]["id"]
    action_id = str(uuid.uuid4())
    move["structure"]["stages"][0]["actions"] = [{
        "id": action_id, "group_id": str(uuid.uuid4()), "version": 1, "kind": "move",
        "role_id": role_id, "path": [{"x": 50.000002, "y": 50}, {"x": 60, "y": 50}],
    }]
    move["fields"][f"action.{action_id}.description"] = "不合法起点"
    cases.append(move)
    boundary = copy.deepcopy(projection)
    second = copy.deepcopy(boundary["structure"]["stages"][0])
    second["id"] = str(uuid.uuid4())
    boundary["structure"]["stages"].append(second)
    boundary["fields"].update({f"stage.{second['id']}.name": "第二阶段",
                                f"stage.{second['id']}.description": "",
                                f"stage.{second['id']}.condition": ""})
    description_keys = []
    for stage in boundary["structure"]["stages"]:
        for _ in range(100):
            identity = str(uuid.uuid4())
            stage["actions"].append({"id": identity, "group_id": str(uuid.uuid4()),
                                      "version": 1, "kind": "text", "role_id": None,
                                      "position": {"x": 50, "y": 50}})
            description_keys.append(f"action.{identity}.description")
            boundary["fields"][description_keys[-1]] = "a"
    remaining = 1048576 - sum(len(value.encode()) for value in boundary["fields"].values())
    for key in description_keys:
        if not remaining:
            break
        amount = min(8000, remaining + 1)
        count, suffix = divmod(amount, 4)
        value = "😀" * count + ["", "a", "aa", "中"][suffix]
        if len(value) > 2000:
            value = "😀" * (count - 1) + "中中"
        remaining -= len(value.encode()) - 1
        boundary["fields"][key] = value
    assert remaining == 0
    cases.append(boundary)
    excessive = copy.deepcopy(boundary)
    excessive["fields"]["tactic.purpose"] += "a"
    cases.append(excessive)
    expected = []
    for case in cases:
        try:
            validate_projection(case, case["structure"]["document_id"])
            expected.append(True)
        except ValueError:
            expected.append(False)
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-validate-projection.mjs"], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await asyncio.to_thread(
        process.communicate, json.dumps(cases, ensure_ascii=False).encode(), timeout=20
    )
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout) == expected == [
        True, False, False, False, True, False
    ], stderr.decode()
