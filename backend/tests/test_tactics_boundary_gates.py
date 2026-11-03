"""Prepared real TCP/Node/PG boundaries; each case requires its own newly provisioned database."""

import hashlib
import os
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import test_tactics_collaboration as tcp
import test_tactics_teams as team_tests
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config
from app.db.tactics_models import ShareLink, Tactic, TacticDocument, TeamInvite, TeamMember
from app.services import tactics_accounts as accounts
from app.services import tactics_documents as documents

event_loop_policy, live = tcp.event_loop_policy, tcp.live
register, write = team_tests.register, team_tests.write
pytestmark = pytest.mark.integration


@pytest.fixture
def database_url(request):
    targets = {
        "copy100": ("TACTICS_COPY100_TEST_DATABASE_URL", "tactics_copy100_selftest"),
        "invites": ("TACTICS_INVITES_TEST_DATABASE_URL", "tactics_invites_selftest"),
    }
    variable, expected = targets[request.param]
    raw = os.environ.get(variable, "")
    if not raw:
        pytest.skip(f"Explicit new {expected} database not arranged")
    target = make_url(raw)
    assert target.host == "127.0.0.1" and target.port == 65402
    assert target.username == "tactics_test" and target.database == expected
    assert target.drivername == "postgresql+psycopg" and not target.query
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "0010_tactics_shares_history")
    finally:
        config.get_settings = previous
    return raw


async def public(client, path, body):
    result = await client.post("/api/v1/shares/" + path, json=body, headers={"origin": tcp.ORIGIN})
    assert result.status_code == 200, f"Public HTTP {result.status_code}"
    assert result.headers["cache-control"] == "no-store"
    return result.json()


async def active_ids(factory, notebook_id):
    async with factory() as db:
        return set((await db.scalars(sa.select(Tactic.id).where(
            Tactic.notebook_id == uuid.UUID(notebook_id), Tactic.status == "active"
        ))).all())


@pytest.mark.parametrize("database_url", ["copy100"], indirect=True)
async def test_whole_share_copies_100_and_rejects_101_atomically(live, tmp_path):
    backend, _, factory, settings = live
    settings.temp_root = str(tmp_path)
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=120) as owner,
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=120) as empty,
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=120) as occupied,
        httpx.AsyncClient(base_url=backend, trust_env=False, timeout=120) as visitor,
    ):
        author, oh = await register(owner, "百条来源作者")
        _, eh = await register(empty, "空目标")
        _, bh = await register(occupied, "已有一条目标")
        source = (await owner.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        target = (await empty.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        busy = (await occupied.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        originals = {}
        # Each source is created by the real API -> actual Node seed -> real PG transaction.
        for index in range(100):
            created = await write(owner, f"/notebooks/{source}/tactics", oh,
                                  {"title": f"百条复制 {index:03d}", "side": "T"}, status=201)
            originals[created["tactic"]["id"]] = created
        source_ids = await active_ids(factory, source)
        assert len(source_ids) == 100
        denied = await write(owner, f"/notebooks/{source}/tactics", oh,
                             {"title": "第101条", "side": "T"}, status=409)
        assert denied["error"]["code"] == "TACTIC_LIMIT_REACHED"
        assert await active_ids(factory, source) == source_ids
        expected = [{"id": key, "revision": item["tactic"]["revision"]}
                    for key, item in originals.items()]
        base = {"notebook_id": source, "tactic_id": None, "allow_copy": True}
        before_links = None
        async with factory() as db:
            before_links = await db.scalar(sa.select(sa.func.count()).select_from(ShareLink).where(
                ShareLink.owner_notebook_id == uuid.UUID(source)))
        # 101 expected entries are rejected by the schema, not a fabricated 101-source fixture.
        await write(owner, "/shares", oh, {**base, "expected_revisions": [
            *expected, {"id": str(uuid.uuid4()), "revision": 0}
        ]}, status=422)
        async with factory() as db:
            assert await db.scalar(sa.select(sa.func.count()).select_from(ShareLink).where(
                ShareLink.owner_notebook_id == uuid.UUID(source))) == before_links
        share = await write(owner, "/shares", oh, {**base, "expected_revisions": expected},
                            status=201)
        grant = (await public(visitor, "exchange", {"token": share["token"]}))["read_grant"]
        manifest = await public(visitor, "read", {"read_grant": grant})
        assert len(manifest["items"]) == 100
        read_ids, cursor, seen_cursors = [], None, set()
        while True:
            page = await public(visitor, "items", {"read_grant": grant,
                "version_id": manifest["version_id"], "cursor": cursor})
            assert page["version_id"] == manifest["version_id"] and page["items"]
            for item in page["items"]:
                assert item["projection"]["structure"]["document_id"] == item["item_id"]
                read_ids.append(item["item_id"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert cursor not in seen_cursors and len(seen_cursors) < 100
            seen_cursors.add(cursor)
        assert read_ids == [item["item_id"] for item in manifest["items"]]
        assert len(set(read_ids)) == 100
        key = str(uuid.uuid4())
        body = {"read_grant": grant, "expected_version_id": share["version_id"]}
        copied = await write(empty, "/shares/copy", eh, body, key=key, status=201)
        assert len(copied["tactics"]) == 100
        copied_ids = {uuid.UUID(item["id"]) for item in copied["tactics"]}
        assert len(copied_ids) == 100 and not copied_ids & source_ids
        assert await active_ids(factory, target) == copied_ids
        for item in copied["tactics"]:
            assert item["notebook_id"] == target
            assert item["source"]["author_display_name"] == author["user"]["display_name"]
            original = originals[item["source"]["tactic_id"]]
            async with factory() as db:
                doc = await db.get(TacticDocument, uuid.UUID(item["id"]))
                old = await db.get(TacticDocument, uuid.UUID(item["source"]["tactic_id"]))
                assert doc.revision == 0 and old.revision == 0 and doc.epoch != old.epoch
                assert hashlib.sha256(doc.yjs_state).digest() == doc.state_digest
                assert hashlib.sha256(documents.json_bytes(doc.projection)).digest() == (
                    doc.projection_digest)
                assert old.projection == original["projection"]
                assert doc.projection["fields"]["tactic.title"] == old.projection["fields"][
                    "tactic.title"]
                assert doc.projection["structure"]["roles"][0]["id"] != (
                    old.projection["structure"]["roles"][0]["id"])
                assert doc.projection["structure"]["stages"][0]["id"] != (
                    old.projection["structure"]["stages"][0]["id"])
        # Original key replays all 100 even though the target is now full; no second batch.
        replay = await write(empty, "/shares/copy", eh, body, key=key, status=201)
        assert {item["id"] for item in replay["tactics"]} == {str(item) for item in copied_ids}
        assert await active_ids(factory, target) == copied_ids
        await write(empty, "/shares/copy", eh, body, status=409)
        assert await active_ids(factory, target) == copied_ids
        one = await write(occupied, f"/notebooks/{busy}/tactics", bh,
                          {"title": "唯一原有正文", "side": "CT"}, status=201)
        blocked = await write(occupied, "/shares/copy", bh, body, status=409)
        assert blocked["error"]["code"] == "TACTIC_LIMIT_REACHED"
        assert await active_ids(factory, busy) == {uuid.UUID(one["tactic"]["id"])}
        assert await active_ids(factory, source) == source_ids
        private = Path(settings.temp_root) / "tactics-share-copy-v1"
        assert private.is_dir()
        assert {path.name for path in private.iterdir()} == {".copy.lock"}


@pytest.mark.parametrize("database_url", ["invites"], indirect=True)
async def test_direct_invite_revoke_expiry_permissions_and_consumption(live):
    backend, _, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as owner,
        httpx.AsyncClient(base_url=backend, trust_env=False) as consumed,
        httpx.AsyncClient(base_url=backend, trust_env=False) as fresh,
    ):
        _, oh = await register(owner)
        member, mh = await register(consumed)
        _, fh = await register(fresh)
        team = (await write(owner, "/teams", oh, {"name": "邀请边界队伍"}, status=201))["team"]
        tid, uid = team["id"], member["user"]["id"]
        invite = await write(owner, f"/teams/{tid}/invites", oh, {}, status=201)
        token = {"token": invite["token"]}
        accept_key = str(uuid.uuid4())
        assert (await write(consumed, "/invites/accept", mh, token,
                            key=accept_key))["member_role"] == "viewer"
        revoke_path = f"/teams/{tid}/invites/{invite['invite_id']}/revoke"
        await write(consumed, revoke_path, mh, {}, status=403)
        await write(owner, f"/teams/{tid}/members/{uid}", oh, {"role": "editor"}, method="PATCH")
        await write(consumed, revoke_path, mh, {}, status=403)
        await write(fresh, revoke_path, fh, {}, status=404)
        assert (await write(consumed, "/invites/accept", mh, token,
                            key=accept_key))["member_role"] == "editor"
        assert (await write(consumed, "/invites/accept", mh, token))["member_role"] == "editor"
        async with factory() as db:
            assert (await db.get(TeamInvite, uuid.UUID(invite["invite_id"]))).uses == 1
        revoke_key = str(uuid.uuid4())
        await write(owner, revoke_path, oh, {}, key=revoke_key)
        async with factory() as db:
            revoked_at = (await db.get(TeamInvite, uuid.UUID(invite["invite_id"]))).revoked_at
        assert revoked_at is not None
        await write(owner, revoke_path, oh, {}, key=revoke_key)
        await write(owner, revoke_path, oh, {})
        for client, headers in [(consumed, mh), (fresh, fh)]:
            preview = await client.post("/api/v1/invites/preview", headers=headers, json=token)
            assert preview.status_code == 404
            assert preview.json()["error"]["code"] == "INVITE_UNAVAILABLE"
            failure = await write(client, "/invites/accept", headers, token, status=404)
            assert failure["error"]["code"] == "INVITE_UNAVAILABLE"
        # A prior successful accept key cannot bypass present revoked authorization.
        await write(consumed, "/invites/accept", mh, token, key=accept_key, status=404)
        assert (await consumed.get(f"/api/v1/teams/{tid}")).json()["my_role"] == "editor"
        listing = (await owner.get(f"/api/v1/teams/{tid}/invites")).json()["items"]
        record = next(item for item in listing if item["invite_id"] == invite["invite_id"])
        assert record["revoked_at"] is not None and "token" not in record
        async with factory() as db:
            stored = await db.get(TeamInvite, uuid.UUID(invite["invite_id"]))
            assert stored.uses == 1 and stored.revoked_at == revoked_at
            assert await db.get(TeamMember, (uuid.UUID(tid), uuid.UUID(uid))) is not None
        await write(consumed, f"/teams/{tid}/members/{uid}", mh, method="DELETE")
        await write(consumed, "/invites/accept", mh, token, status=404)
        expiring = await write(owner, f"/teams/{tid}/invites", oh, {}, status=201)
        expiry_token = {"token": expiring["token"]}
        expiry_key = str(uuid.uuid4())
        await write(consumed, "/invites/accept", mh, expiry_token, key=expiry_key)
        # Only this newly created row's expiry is injected; no global clock change or natural wait.
        async with factory() as db, db.begin():
            stored = await db.get(TeamInvite, uuid.UUID(expiring["invite_id"]))
            stored.expires_at = accounts.now() - timedelta(seconds=1)
        for client, headers in [(consumed, mh), (fresh, fh)]:
            preview = await client.post("/api/v1/invites/preview", headers=headers,
                                        json=expiry_token)
            assert preview.status_code == 404
            assert preview.json()["error"]["code"] == "INVITE_UNAVAILABLE"
            await write(client, "/invites/accept", headers, expiry_token, status=404)
        await write(consumed, "/invites/accept", mh, expiry_token, key=expiry_key, status=404)
        async with factory() as db:
            stored = await db.get(TeamInvite, uuid.UUID(expiring["invite_id"]))
            assert stored.uses == 1 and stored.revoked_at is None
            assert (await db.get(TeamMember, (uuid.UUID(tid), uuid.UUID(uid)))).role == "viewer"
            assert await db.scalar(sa.select(sa.func.count()).select_from(TeamMember).where(
                TeamMember.team_id == uuid.UUID(tid))) == 2
