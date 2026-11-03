"""Prepared pagination contract checks, restricted to the new pagination database."""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import test_tactics_collaboration as tcp
import test_tactics_teams as team_tests
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config

event_loop_policy, live = tcp.event_loop_policy, tcp.live
register, write = team_tests.register, team_tests.write
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_PAGINATION_TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("Explicit new tactics_pagination_selftest database not arranged")
    target = make_url(raw)
    assert target.host == "127.0.0.1" and target.port == 65402
    assert target.username == "tactics_test" and target.database == "tactics_pagination_selftest"
    assert target.drivername == "postgresql+psycopg" and not target.query
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "0010_tactics_shares_history")
    finally:
        config.get_settings = previous
    return raw


@asynccontextmanager
async def actors(live):
    backend, _, _, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=30) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=30) as b,
    ):
        _, ah = await register(a, "分页队长")
        other, bh = await register(b, "分页成员")
        personal = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        second = (await b.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        teams = [(await write(a, "/teams", ah, {"name": f"分页队伍{i}"}, status=201))["team"]
                 for i in range(2)]
        invite = await write(a, f"/teams/{teams[0]['id']}/invites", ah, {}, status=201)
        await write(b, "/invites/accept", bh, {"token": invite["token"]})
        yield SimpleNamespace(a=a, b=b, ah=ah, bh=bh, other_id=other["user"]["id"],
                              personal=personal, second=second, teams=teams)


async def page(client, path, params, *, status=200):
    result = await client.get("/api/v1" + path, params=params)
    assert result.status_code == status, f"Pagination HTTP {result.status_code}"
    assert result.headers["cache-control"] == "no-store"
    if status == 422:
        assert result.json()["error"]["code"] == "VALIDATION_ERROR"
    return result.json()


async def traverse(client, path, count, *, key="id", timestamp="created_at", params=None):
    params = params or {}
    all_rows = (await page(client, path, {**params, "limit": 50}))["items"]
    assert len(all_rows) == count and count >= 2
    ordered = sorted(all_rows, key=lambda row: (
        datetime.fromisoformat(row[timestamp].replace("Z", "+00:00")), uuid.UUID(row[key]).int
    ), reverse=True)
    assert [row[key] for row in all_rows] == [row[key] for row in ordered]
    cursor, first_cursor, seen = None, None, []
    for index in range(count):
        result = await page(client, path, {**params, "limit": 1,
                                          **({"cursor": cursor} if cursor else {})})
        assert len(result["items"]) == 1
        seen.append(result["items"][0][key])
        cursor = result["next_cursor"]
        if index == 0:
            first_cursor = cursor
        assert (cursor is None) == (index == count - 1)
    assert len(set(seen)) == count and seen == [row[key] for row in all_rows]
    return first_cursor, all_rows


async def reject_cursor(client, path, cursor, *, params=None, status=422):
    return await page(client, path, {**(params or {}), "limit": 1, "cursor": cursor},
                      status=status)


async def create_tactic(f, notebook_id, title):
    return await write(f.a, f"/notebooks/{notebook_id}/tactics", f.ah,
                       {"title": title, "side": "T"}, status=201)


async def transfer(f):
    await write(f.a, f"/teams/{f.teams[0]['id']}/ownership", f.ah,
                {"user_id": f.other_id})


async def test_tactics_paging_binds_notebook_filters_but_not_authorized_actor(live):
    async with actors(live) as f:
        created = [await create_tactic(f, f.personal, f"分页正文{i}") for i in range(6)]
        for item in created[:3]:
            await write(f.a, f"/tactics/{item['tactic']['id']}/archive", f.ah,
                        {"expected_revision": item["tactic"]["revision"]})
        path = f"/notebooks/{f.personal}/tactics"
        cursor, _ = await traverse(f.a, path, 3, timestamp="updated_at")
        deleted, _ = await traverse(f.a, path, 3, timestamp="updated_at",
                                    params={"deleted": "true"})
        searched, _ = await traverse(f.a, path, 3, timestamp="updated_at", params={"q": "分页"})
        await reject_cursor(f.a, path, cursor, params={"deleted": "true"})
        await reject_cursor(f.a, path, deleted)
        await reject_cursor(f.a, path, searched, params={"q": "另一个条件"})
        await reject_cursor(f.a, f"/notebooks/{f.teams[0]['notebook_id']}/tactics", cursor)
        await reject_cursor(f.b, path, cursor, status=404)  # Permission check, not actor binding.
        await reject_cursor(f.b, f"/notebooks/{f.second}/tactics", cursor)
        team_path = f"/notebooks/{f.teams[0]['notebook_id']}/tactics"
        for i in range(3):
            await create_tactic(f, f.teams[0]["notebook_id"], f"共用正文{i}")
        shared, rows = await traverse(f.a, team_path, 3, timestamp="updated_at")
        # Tactics cursor has n/deleted/q scope, intentionally no actor binding.
        allowed = await page(f.b, team_path, {"limit": 1, "cursor": shared})
        assert allowed["items"][0]["id"] == rows[1]["id"]


async def test_team_and_invite_paging_rejects_existing_actor_team_filter_mismatch(live):
    async with actors(live) as f:
        for i in range(3):
            made = await write(f.a, "/teams", f.ah, {"name": f"归档分页{i}"}, status=201)
            await write(f.a, f"/teams/{made['team']['id']}/archive", f.ah, {})
        active, _ = await traverse(f.a, "/teams", 2)
        archived, _ = await traverse(f.a, "/teams/archived", 3)
        await reject_cursor(f.a, "/teams/archived", active)
        await reject_cursor(f.a, "/teams", archived)
        await reject_cursor(f.b, "/teams", active)
        for _ in range(2):
            await write(f.a, f"/teams/{f.teams[0]['id']}/invites", f.ah, {}, status=201)
        path = f"/teams/{f.teams[0]['id']}/invites"
        cursor, rows = await traverse(f.a, path, 3, key="invite_id")
        assert all("token" not in row for row in rows)
        await reject_cursor(f.a, f"/teams/{f.teams[1]['id']}/invites", cursor)
        await reject_cursor(f.b, path, cursor, status=403)
        await transfer(f)
        # Newly authorized owner, same resource: existing actor binding rejects the old cursor.
        await reject_cursor(f.b, path, cursor)
        assert len((await page(f.b, path, {"limit": 50}))["items"]) == 3
        await reject_cursor(f.a, path, cursor, status=403)


async def test_share_management_and_history_paging_scope_and_final_page(live):
    async with actors(live) as f:
        notebook = f.teams[0]["notebook_id"]
        first = await create_tactic(f, notebook, "有历史正文")
        second = await create_tactic(f, notebook, "另一份正文")
        identity, revision = first["tactic"]["id"], first["tactic"]["revision"]
        # Generate distinct history epochs/revisions through existing archive/restore APIs.
        for _ in range(2):
            await write(f.a, f"/tactics/{identity}/archive", f.ah,
                        {"expected_revision": revision})
            restored = await write(f.a, f"/tactics/{identity}/restore", f.ah, {})
            revision = restored["revision"]
        shares = []
        for _ in range(3):
            shares.append(await write(f.a, "/shares", f.ah, {"notebook_id": notebook,
                "tactic_id": identity, "allow_copy": False,
                "expected_revisions": [{"id": identity, "revision": revision}]}, status=201))
        await write(f.a, f"/shares/{shares[0]['id']}/revoke", f.ah, {})
        path = f"/notebooks/{notebook}/shares"
        cursor, rows = await traverse(f.a, path, 3)
        assert all("token" not in row for row in rows)
        assert next(row for row in rows if row["id"] == shares[0]["id"])["revoked_at"]
        await reject_cursor(f.a, f"/notebooks/{f.teams[1]['notebook_id']}/shares", cursor)
        await reject_cursor(f.b, path, cursor, status=403)
        history_path = f"/tactics/{identity}/versions"
        history_cursor, versions = await traverse(f.a, history_path, 3)
        assert {row["reason"] for row in versions} == {"archive", "publish"}
        await reject_cursor(f.a, f"/tactics/{second['tactic']['id']}/versions", history_cursor)
        await reject_cursor(f.b, history_path, history_cursor, status=403)
        await transfer(f)
        await reject_cursor(f.b, path, cursor)
        await reject_cursor(f.b, history_path, history_cursor)
        assert len((await page(f.b, path, {"limit": 50}))["items"]) == 3
        assert len((await page(f.b, history_path, {"limit": 50}))["items"]) == 3
        await reject_cursor(f.a, path, cursor, status=403)
        await reject_cursor(f.a, history_path, history_cursor, status=403)
