"""Additional real event representatives on the existing dedicated 0011 audit test database."""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import test_tactics_audit_pg as core
import test_tactics_boundary_gates as boundary
import test_tactics_collaboration as tcp

from app.db import tactics_models as m
from app.services import tactics_history as history

database_url, event_loop_policy, live = core.database_url, tcp.event_loop_policy, tcp.live
register, write, events = core.register, core.write, core.events
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def private_spool(live, tmp_path):
    live[3].temp_root = str(tmp_path)


async def test_account_success_events_and_noop_profile(live):
    backend, _, factory, _ = live
    password = "team-private-test-password-2026"
    changed = "audit-new-public-password-2026"
    final = "audit-recovered-public-password-2026"
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as client:
        account, headers = await register(client, "账号审计fixture")
        actor = account["user"]["id"]
        username = account["user"]["username"]
        login = await write(
            client, "/auth/login", headers, {"username": username, "password": password}
        )
        headers["x-csrf-token"] = login["csrf_token"]
        await write(
            client,
            "/auth/profile",
            headers,
            {"display_name": "AUDIT_NAME_SENTINEL"},
            method="PATCH",
        )
        before = len(await events(factory, actor))
        await write(
            client,
            "/auth/profile",
            headers,
            {"display_name": "AUDIT_NAME_SENTINEL"},
            method="PATCH",
        )
        assert len(await events(factory, actor)) == before
        result = await write(
            client,
            "/auth/password",
            headers,
            {"current_password": password, "new_password": changed},
        )
        headers["x-csrf-token"] = result["csrf_token"]
        code = (await write(client, "/auth/recovery-code", headers, {"current_password": changed}))[
            "recovery_code"
        ]
        await write(
            client,
            "/auth/recover",
            headers,
            {"username": username, "recovery_code": code, "new_password": final},
        )
        login = await write(
            client, "/auth/login", {"origin": tcp.ORIGIN}, {"username": username, "password": final}
        )
        headers["x-csrf-token"] = login["csrf_token"]
        await write(client, "/auth/logout", headers, {})
        await write(client, "/auth/logout", headers, {})
        rows = await events(factory, actor)
        names = [row.action for row in rows]
        assert set(names) == {
            "account.register",
            "account.login",
            "account.logout",
            "account.profile.update",
            "account.password.change",
            "account.recovery.rotate",
            "account.recover",
        }
        assert names.count("account.login") == 2 and names.count("account.logout") == 1
        serialized = json.dumps(
            [{key: str(getattr(row, key)) for key in row.__table__.columns.keys()} for row in rows]
        )
        for secret in [password, changed, final, code, username, "AUDIT_NAME_SENTINEL"]:
            assert secret not in serialized


async def test_team_tactic_share_copy_and_history_success_representatives(live):
    backend, _, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
    ):
        owner, ah = await register(a)
        member, bh = await register(b)
        oid, uid = owner["user"]["id"], member["user"]["id"]
        book = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        team = (await write(a, "/teams", ah, {"name": "审计队伍"}, status=201))["team"]
        tid = team["id"]
        await write(a, f"/teams/{tid}", ah, {"name": "审计队伍改名"}, method="PATCH")
        invite = await write(a, f"/teams/{tid}/invites", ah, {}, status=201)
        await write(b, "/invites/accept", bh, {"token": invite["token"]})
        await write(a, f"/teams/{tid}/members/{uid}", ah, {"role": "editor"}, method="PATCH")
        await write(a, f"/teams/{tid}/invites/{invite['invite_id']}/revoke", ah, {})
        await write(a, f"/teams/{tid}/invites/{invite['invite_id']}/revoke", ah, {})
        tactic = await write(
            a, f"/notebooks/{book}/tactics", ah, {"title": "审计资源", "side": "T"}, status=201
        )
        identity = tactic["tactic"]["id"]
        await write(
            a,
            f"/tactics/{identity}/copy",
            ah,
            {"target_notebook_id": team["notebook_id"]},
            status=201,
        )
        publication = {
            "notebook_id": book,
            "tactic_id": identity,
            "allow_copy": False,
            "expected_revisions": [{"id": identity, "revision": 0}],
        }
        share = await write(a, "/shares", ah, publication, status=201)
        await write(
            a,
            f"/shares/{share['id']}/publish",
            ah,
            {"allow_copy": False, "expected_revisions": publication["expected_revisions"]},
        )
        await write(a, f"/shares/{share['id']}/revoke", ah, {})
        await write(a, f"/shares/{share['id']}/revoke", ah, {})
        version = (await a.get(f"/api/v1/tactics/{identity}/versions")).json()["items"][0]
        await write(
            a,
            f"/tactics/{identity}/versions/{version['id']}/restore-copy",
            ah,
            {"target_notebook_id": book},
            status=201,
        )
        await write(a, f"/tactics/{identity}/archive", ah, {"expected_revision": 0})
        await write(a, f"/tactics/{identity}/restore", ah, {})
        await write(a, f"/tactics/{identity}/restore", ah, {})
        await write(a, f"/teams/{tid}/members/{uid}", ah, method="DELETE")
        fresh = await write(a, f"/teams/{tid}/invites", ah, {}, status=201)
        await write(b, "/invites/accept", bh, {"token": fresh["token"]})
        await write(a, f"/teams/{tid}/ownership", ah, {"user_id": uid})
        await write(a, f"/teams/{tid}/members/{oid}", ah, method="DELETE")
        await write(b, f"/teams/{tid}/archive", bh, {})
        await write(b, f"/teams/{tid}/restore", bh, {})
        await write(b, f"/teams/{tid}/restore", bh, {})
        rows = [*await events(factory, oid), *await events(factory, uid)]
        names = [row.action for row in rows]
        assert {
            "team.create",
            "team.rename",
            "team.invite.create",
            "team.invite.accept",
            "team.invite.revoke",
            "team.member.role",
            "team.member.remove",
            "team.member.leave",
            "team.transfer",
            "team.archive",
            "team.restore",
            "tactic.create",
            "tactic.copy",
            "tactic.archive",
            "tactic.restore",
            "tactic.restore_copy",
            "share.create",
            "share.publish",
            "share.revoke",
        } <= set(names)
        for action in ["team.invite.revoke", "share.revoke", "tactic.restore", "team.restore"]:
            assert names.count(action) == 1
        assert all(row.result_code == "SUCCESS" for row in rows)


async def test_real_ws_commit_duplicate_and_new_periodic_checkpoint(live):
    backend, seed, factory, _ = live
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-audit-smoke.mjs"],
        cwd=tcp.ROOT,
        env={
            **os.environ,
            "FORMAL_TEST_BACKEND": backend,
            "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
            "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
            "AUDIT_TEST_DATABASE_NAME": "tactics_audit_selftest",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=30)
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)
    rows = await events(factory, result["actor_id"])
    document_events = [row for row in rows if row.action.startswith("document.")]
    assert {str(row.resource_id) for row in document_events} == set(result["operations"])
    assert sorted(row.action for row in document_events) == [
        "document.role.add",
        "document.text.update",
    ]
    assert all("AUDIT_BODY_SENTINEL" not in str(row.__dict__) for row in rows)
    async with factory() as db, db.begin():
        doc = await db.get(m.TacticDocument, uuid.UUID(result["tactic_id"]))
        actor = await db.scalar(
            sa.select(m.TacticOperation.actor_id).where(
                m.TacticOperation.tactic_id == doc.tactic_id,
                m.TacticOperation.epoch == doc.epoch,
                m.TacticOperation.revision == doc.revision,
            )
        )
        first = await history.save_history(db, doc, actor, "periodic", audit_actor_id=actor)
        again = await history.save_history(db, doc, actor, "periodic", audit_actor_id=actor)
        assert first.id == again.id
    rows = await events(factory, result["actor_id"])
    assert sum(row.action == "history.checkpoint" for row in rows) == 1


async def test_100_copy_events_commit_together_and_keep_original_idempotency(live, tmp_path):
    factory = live[2]
    async with factory() as db:
        before = await db.scalar(
            sa.select(sa.func.count())
            .select_from(m.TacticAuditEvent)
            .where(m.TacticAuditEvent.action == "tactic.copy.share")
        )
    await boundary.test_whole_share_copies_100_and_rejects_101_atomically(live, tmp_path)
    async with factory() as db:
        after = await db.scalar(
            sa.select(sa.func.count())
            .select_from(m.TacticAuditEvent)
            .where(m.TacticAuditEvent.action == "tactic.copy.share")
        )
    assert after - before == 100
    assert {path.name for path in (Path(tmp_path) / "tactics-share-copy-v1").iterdir()} == {
        ".copy.lock"
    }


async def test_batch_copy_audit_insert_failure_rolls_back_all_copies(live):
    backend, _, factory, settings = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
    ):
        _, ah = await register(a)
        receiver, bh = await register(b)
        actor = receiver["user"]["id"]
        source = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        target = (await b.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        expected = []
        for i in range(2):
            made = await write(
                a,
                f"/notebooks/{source}/tactics",
                ah,
                {"title": f"审计批回滚{i}", "side": "T"},
                status=201,
            )
            expected.append({"id": made["tactic"]["id"], "revision": 0})
        share = await write(
            a,
            "/shares",
            ah,
            {
                "notebook_id": source,
                "tactic_id": None,
                "allow_copy": True,
                "expected_revisions": expected,
            },
            status=201,
        )
        exchange = await a.post(
            "/api/v1/shares/exchange",
            headers={"origin": tcp.ORIGIN},
            json={"token": share["token"]},
        )
        assert exchange.status_code == 200
        trigger = "audit_copy_fault_" + uuid.uuid4().hex
        async with factory() as db, db.begin():
            await db.execute(
                sa.text(
                    f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'isolated audit copy fault'; END; $$"
                )
            )
            await db.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON tactic_audit_events "
                    f"FOR EACH ROW WHEN (NEW.actor_id='{actor}'::uuid) EXECUTE FUNCTION {trigger}()"
                )
            )
        try:
            response = await b.post(
                "/api/v1/shares/copy",
                headers={**bh, "idempotency-key": str(uuid.uuid4())},
                json={
                    "read_grant": exchange.json()["read_grant"],
                    "expected_version_id": share["version_id"],
                },
            )
            assert response.status_code in {500, 503}
            async with factory() as db:
                assert (
                    await db.scalar(
                        sa.select(sa.func.count())
                        .select_from(m.Tactic)
                        .where(m.Tactic.notebook_id == uuid.UUID(target))
                    )
                    == 0
                )
            assert [row.action for row in await events(factory, actor)] == ["account.register"]
            assert {
                path.name for path in (Path(settings.temp_root) / "tactics-share-copy-v1").iterdir()
            } == {".copy.lock"}
        finally:
            async with factory() as db, db.begin():
                await db.execute(
                    sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON tactic_audit_events")
                )
                await db.execute(sa.text(f"DROP FUNCTION IF EXISTS {trigger}()"))
