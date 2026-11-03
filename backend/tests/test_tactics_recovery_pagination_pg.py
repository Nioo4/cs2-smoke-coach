"""Prepared real PostgreSQL recovery pagination checks; explicit isolated DB only."""

import os
import uuid
from datetime import timedelta

import httpx
import pytest
import test_tactics_collaboration as tcp
import test_tactics_teams as team_tests
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config
from app.db.tactics_models import TacticRecoveryItem
from app.services import tactics_accounts as accounts

event_loop_policy, live = tcp.event_loop_policy, tcp.live
register, write = team_tests.register, team_tests.write
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_RECOVERY_PAGINATION_TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("Explicit new tactics_recovery_pagination_selftest DB not arranged")
    target = make_url(raw)
    assert target.host == "127.0.0.1" and target.port == 65402
    assert target.username == "tactics_test"
    assert target.database == "tactics_recovery_pagination_selftest"
    assert target.drivername == "postgresql+psycopg" and not target.query
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        config.get_settings = previous
    return raw


async def page(client, identity, params=None, *, status=200):
    response = await client.get(f"/api/v1/tactics/{identity}/recoveries", params=params)
    assert response.status_code == status, f"Recovery pagination HTTP {response.status_code}"
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def insert_recoveries(factory, view, user_id, at, count):
    base = uuid.uuid4().int & ~255
    identities = [uuid.UUID(int=base + index) for index in range(1, count + 1)]
    async with factory() as db, db.begin():
        db.add_all([TacticRecoveryItem(
            id=identities[index - 1], tactic_id=uuid.UUID(view["tactic"]["id"]),
            source_epoch=uuid.UUID(view["epoch"]), entity_id=uuid.uuid4(), kind="action",
            snapshot={}, texts={}, deleted_by=uuid.UUID(user_id), created_at=at,
            expires_at=at + (timedelta(seconds=1) if index == 1 else timedelta(days=1)),
        ) for index in range(1, count + 1)])
        db.add(TacticRecoveryItem(
            tactic_id=uuid.UUID(view["tactic"]["id"]), source_epoch=uuid.UUID(view["epoch"]),
            entity_id=uuid.uuid4(), kind="stage", snapshot={}, texts={},
            deleted_by=uuid.UUID(user_id), created_at=at + timedelta(seconds=1), expires_at=at,
        ))
    return identities


async def test_real_keyset_ties_limits_expiration_and_unauthorized_resource(live, monkeypatch):
    backend, _, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=30) as owner,
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=30) as stranger,
    ):
        user, headers = await register(owner, "恢复分页作者")
        await register(stranger, "恢复分页无权限")
        notebook = (await owner.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        view = await write(owner, f"/notebooks/{notebook}/tactics", headers,
                           {"title": "恢复分页边界", "side": "T"}, status=201)
        second = await write(owner, f"/notebooks/{notebook}/tactics", headers,
                             {"title": "游标隔离", "side": "CT"}, status=201)
        identity = view["tactic"]["id"]
        at = accounts.now()
        monkeypatch.setattr(accounts, "now", lambda: at)
        identities = await insert_recoveries(factory, view, user["user"]["id"], at, 105)
        initial = await page(owner, identity)
        assert len(initial["items"]) == 20 and initial["next_cursor"]
        maximum = await page(owner, identity, {"limit": 100})
        assert len(maximum["items"]) == 100 and maximum["next_cursor"]
        tail = await page(owner, identity, {"limit": 100, "cursor": maximum["next_cursor"]})
        assert len(tail["items"]) == 5 and tail["next_cursor"] is None
        assert set(initial["items"][0]) == {
            "id", "kind", "entity_id", "created_at", "expires_at",
        }
        cursor = initial["next_cursor"]
        assert (await page(owner, second["tactic"]["id"], {"cursor": cursor}, status=422))[
            "error"]["code"] == "VALIDATION_ERROR"
        # Authorization precedes cursor decoding, including a malformed cursor.
        await page(stranger, identity, {"cursor": "not-base64"}, status=404)
        for invalid in [{"limit": 0}, {"limit": 101}, {"cursor": "x" * 2049}]:
            await page(owner, identity, invalid, status=422)
        first = await page(owner, identity, {"limit": 17})
        seen = [item["id"] for item in first["items"]]
        cursor = first["next_cursor"]
        at += timedelta(seconds=2)  # The not-yet-read lowest UUID expires between pages.
        while cursor:
            result = await page(owner, identity, {"limit": 17, "cursor": cursor})
            seen.extend(item["id"] for item in result["items"])
            cursor = result["next_cursor"]
        assert seen == [str(identity) for identity in reversed(identities[1:])]


async def test_team_editor_actor_cursor_binding_and_viewer_denial(live):
    backend, _, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=30) as owner,
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=30) as member,
    ):
        user, headers = await register(owner, "恢复分页队长")
        peer, peer_headers = await register(member, "恢复分页队员")
        team = (await write(owner, "/teams", headers, {"name": "恢复分页队伍"},
                            status=201))["team"]
        invitation = await write(owner, f"/teams/{team['id']}/invites", headers, {}, status=201)
        await write(member, "/invites/accept", peer_headers, {"token": invitation["token"]})
        view = await write(owner, f"/notebooks/{team['notebook_id']}/tactics", headers,
                           {"title": "有权限也不能跨用户复用游标", "side": "CT"}, status=201)
        identity = view["tactic"]["id"]
        # Unique test-row IDs across both test cases, using new resource-local random UUIDs.
        at = accounts.now() - timedelta(seconds=10)
        async with factory() as db, db.begin():
            db.add_all([TacticRecoveryItem(
                tactic_id=uuid.UUID(identity), source_epoch=uuid.UUID(view["epoch"]),
                entity_id=uuid.uuid4(), kind="action", snapshot={}, texts={},
                deleted_by=uuid.UUID(user["user"]["id"]), created_at=at,
                expires_at=at + timedelta(days=1),
            ) for _ in range(3)])
        cursor = (await page(owner, identity, {"limit": 1}))["next_cursor"]
        await page(member, identity, {"cursor": "not-base64"}, status=403)
        await write(owner, f"/teams/{team['id']}/members/{peer['user']['id']}", headers,
                    {"role": "editor"}, method="PATCH")
        await page(member, identity, {"cursor": cursor}, status=422)
        assert len((await page(member, identity))["items"]) == 3
