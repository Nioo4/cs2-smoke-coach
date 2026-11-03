"""T3 real TCP/Node/PostgreSQL checks, restricted to the separate teams database."""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import uuid

import httpx
import pytest
import sqlalchemy as sa
import test_tactics_accounts as account_tests
import test_tactics_collaboration as tcp
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config
from app.db.tactics_models import PersonalNotebook
from app.services import tactics_accounts as accounts

event_loop_policy, live = tcp.event_loop_policy, tcp.live
app_client = account_tests.app_client
test_credentials_security_regression = (
    account_tests.test_password_and_recovery_atomically_revoke_all_sessions
)
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_TEAMS_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.database == "tactics_teams_selftest"
    assert parsed.port == 65402
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        config.get_settings = previous
    return raw


async def register(client, label="队伍测试"):
    accounts._RATE_WINDOWS.clear()
    result = await client.post(
        "/api/v1/auth/register",
        headers={"origin": tcp.ORIGIN},
        json={
            "username": "team_" + uuid.uuid4().hex[:18],
            "display_name": label,
            "password": "team-private-test-password-2026",
        },
    )
    assert result.status_code == 201, result.text
    return result.json(), {"origin": tcp.ORIGIN, "x-csrf-token": result.json()["csrf_token"]}


async def write(client, path, headers, body=None, method="POST", key=None, status=200):
    result = await client.request(
        method,
        "/api/v1" + path,
        headers={**headers, "idempotency-key": key or str(uuid.uuid4())},
        **({"json": body} if body is not None else {}),
    )
    assert result.status_code == status, result.text
    assert result.headers["cache-control"] == "no-store"
    return result.json()


async def test_team_directory_roles_invitations_transfer_archive_and_copy(live):
    backend, _, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
    ):
        owner, ah = await register(a, "队长")
        other, bh = await register(b, "队友")
        personal = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]
        created = await write(a, "/teams", ah, {"name": "独立队伍"}, status=201)
        team, notebook = created["team"], created["notebook"]
        assert notebook["user_id"] is None and notebook["kind"] == "team"
        assert (await b.get(f"/api/v1/teams/{team['id']}")).status_code == 404
        key = str(uuid.uuid4())
        invite = await write(a, f"/teams/{team['id']}/invites", ah, {}, key=key, status=201)
        retry = await write(a, f"/teams/{team['id']}/invites", ah, {}, key=key)
        assert retry["invite_id"] == invite["invite_id"]
        assert retry["token"] is None and retry["token_available"] is False
        token = {"token": invite["token"]}
        preview = await write(b, "/invites/preview", bh, token)
        assert preview["already_member"] is False
        accepted = await write(b, "/invites/accept", bh, token)
        assert accepted["member_role"] == "viewer"
        detail = (await b.get(f"/api/v1/teams/{team['id']}")).json()
        assert detail["permissions"] == {
            "read": True,
            "create": False,
            "manage": False,
            "publish": False,
        }
        assert "username" not in str(detail["members"])
        await write(
            b,
            f"/notebooks/{notebook['id']}/tactics",
            bh,
            {"title": "不可创建", "side": "T"},
            status=403,
        )
        await write(
            a,
            f"/teams/{team['id']}/members/{other['user']['id']}",
            ah,
            {"role": "editor"},
            method="PATCH",
        )
        assert (await write(b, "/invites/accept", bh, token))["member_role"] == "editor"
        tactic = await write(
            b,
            f"/notebooks/{notebook['id']}/tactics",
            bh,
            {"title": "队伍正文", "side": "T"},
            status=201,
        )
        assert tactic["permissions"]["edit"] is True and tactic["permissions"]["manage"] is False
        listing = (await b.get(f"/api/v1/notebooks/{notebook['id']}/tactics")).json()
        assert listing["permissions"]["create"] is True
        assert listing["items"][0]["permissions"]["publish"] is False
        copied = await write(
            a,
            f"/tactics/{tactic['tactic']['id']}/copy",
            ah,
            {"target_notebook_id": personal["id"]},
            status=201,
        )
        assert copied["notebook_id"] == personal["id"]
        assert copied["source"]["author_display_name"] == "队友"
        projection = (await a.get(f"/api/v1/tactics/{copied['id']}")).json()["projection"]
        assert (
            projection["structure"]["roles"][0]["id"]
            != (tactic["projection"]["structure"]["roles"][0]["id"])
        )
        await write(b, f"/teams/{team['id']}/members/{other['user']['id']}", bh, method="DELETE")
        await write(b, "/invites/accept", bh, token, status=404)
        fresh = await write(a, f"/teams/{team['id']}/invites", ah, {}, status=201)
        await write(b, "/invites/accept", bh, {"token": fresh["token"]})
        await write(a, f"/teams/{team['id']}/ownership", ah, {"user_id": other["user"]["id"]})
        assert (await a.get(f"/api/v1/teams/{team['id']}")).json()["my_role"] == "editor"
        await write(a, f"/teams/{team['id']}/archive", ah, {}, status=403)
        await write(b, f"/teams/{team['id']}/archive", bh, {})
        assert (await a.get(f"/api/v1/tactics/{tactic['tactic']['id']}")).status_code == 404
        assert (await b.get("/api/v1/teams/archived")).json()["items"][0]["id"] == team["id"]
        await write(b, f"/teams/{team['id']}/restore", bh, {})
        await write(a, "/invites/preview", ah, {"token": fresh["token"]}, status=404)
        async with factory() as db:
            saved = await db.get(PersonalNotebook, uuid.UUID(personal["id"]))
            assert saved.user_id == uuid.UUID(owner["user"]["id"]) and saved.team_id is None


async def test_real_team_websocket_permissions_and_revocation(live, database_url):
    backend, seed, _, _ = live
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-teams-smoke.mjs"],
        cwd=tcp.ROOT,
        env={
            **os.environ,
            "FORMAL_TEST_BACKEND": backend,
            "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
            "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
            "FORMAL_TEST_INTERNAL_KEY": tcp.KEY,
            "TEAMS_TEST_DATABASE_URL": database_url.replace(
                "postgresql+psycopg://", "postgresql://"
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=45)
        assert process.returncode == 0, stderr.decode()
        assert b"FORMAL_TEAMS_HTTP_WS_PG_PASS" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)


async def test_opposite_team_copies_lock_domains_in_stable_order(live):
    backend, _, _, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
    ):
        _, ah = await register(a)
        member, bh = await register(b)
        created = []
        for _ in range(2):
            team = await write(a, "/teams", ah, {"name": "交叉复制"}, status=201)
            tid, nid = team["team"]["id"], team["notebook"]["id"]
            invite = await write(a, f"/teams/{tid}/invites", ah, {}, status=201)
            await write(b, "/invites/accept", bh, {"token": invite["token"]})
            await write(
                a,
                f"/teams/{tid}/members/{member['user']['id']}",
                ah,
                {"role": "editor"},
                method="PATCH",
            )
            tactic = await write(
                a, f"/notebooks/{nid}/tactics", ah, {"title": "双向复制", "side": "CT"}, status=201
            )
            created.append((tactic["tactic"]["id"], nid))
        results = await asyncio.wait_for(
            asyncio.gather(
                write(
                    a,
                    f"/tactics/{created[0][0]}/copy",
                    ah,
                    {"target_notebook_id": created[1][1]},
                    status=201,
                ),
                write(
                    b,
                    f"/tactics/{created[1][0]}/copy",
                    bh,
                    {"target_notebook_id": created[0][1]},
                    status=201,
                ),
            ),
            10,
        )
        assert results[0]["id"] != results[1]["id"]


async def test_invitation_member_limit_is_atomic_and_retries_do_not_consume(live):
    backend, _, factory, _ = live
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as owner:
        _, headers = await register(owner)
        made = await write(owner, "/teams", headers, {"name": "人数边界"}, status=201)
        tid = made["team"]["id"]
        invite = await write(owner, f"/teams/{tid}/invites", headers, {}, status=201)
        clients = []
        try:
            # 18 actual accept transactions fill to 19; the final slot has two competitors.
            for _ in range(20):
                client = httpx.AsyncClient(base_url=backend, trust_env=False)
                identity, ch = await register(client)
                clients.append((client, ch, identity["user"]["id"]))
            for client, ch, _ in clients[:18]:
                await write(client, "/invites/accept", ch, {"token": invite["token"]})

            async def accept(item):
                client, ch, _ = item
                return await client.post(
                    "/api/v1/invites/accept",
                    json={"token": invite["token"]},
                    headers={**ch, "idempotency-key": str(uuid.uuid4())},
                )

            attempts = await asyncio.gather(*(accept(item) for item in clients[18:]))
            assert sorted(result.status_code for result in attempts) == [200, 409]
            failed = next(result for result in attempts if result.status_code == 409)
            assert failed.json()["error"]["code"] == "TEAM_MEMBER_LIMIT_REACHED"
            client, ch, removed_id = clients[0]
            await write(client, "/invites/accept", ch, {"token": invite["token"]})
            async with factory() as db:
                from app.db.tactics_models import TeamInvite, TeamMember

                saved = await db.get(TeamInvite, uuid.UUID(invite["invite_id"]))
                assert saved.uses == 19
                assert (
                    await db.scalar(
                        sa.select(sa.func.count())
                        .select_from(TeamMember)
                        .where(TeamMember.team_id == uuid.UUID(tid))
                    )
                    == 20
                )
            await write(owner, f"/teams/{tid}/members/{removed_id}", headers, method="DELETE")
            failed_index = next(index for index, result in enumerate(attempts)
                                if result.status_code == 409)
            remaining = clients[18 + failed_index]
            await write(remaining[0], "/invites/accept", remaining[1],
                        {"token": invite["token"]})
            await write(owner, f"/teams/{tid}/members/{clients[1][2]}", headers, method="DELETE")
            outsider = httpx.AsyncClient(base_url=backend, trust_env=False)
            identity, oh = await register(outsider)
            clients.append((outsider, oh, identity["user"]["id"]))
            await write(outsider, "/invites/accept", oh, {"token": invite["token"]}, status=404)
            await write(remaining[0], "/invites/accept", remaining[1],
                        {"token": invite["token"]})
            async with factory() as db:
                assert (await db.get(TeamInvite, uuid.UUID(invite["invite_id"])) ).uses == 20
        finally:
            for client, _, _ in clients:
                await client.aclose()


def test_upgrade_0008_to_0009_preserves_personal_ids_and_real_yjs(database_url):
    # Own disposable schema in the guarded teams DB; never downgrade the populated public schema.
    schema = "t3_migration_" + uuid.uuid4().hex
    engine = sa.create_engine(database_url)
    scoped = make_url(database_url).update_query_dict({"options": f"-csearch_path={schema}"})
    scoped_engine = sa.create_engine(scoped)
    user_id, notebook_id, tactic_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    node = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "import {createFormalSeed} from './src/formal-document.mjs';"
            "process.stdout.write(JSON.stringify(createFormalSeed({document_id:process.argv[1],"
            "title:'预置中文😀',side:'T'})));",
            str(tactic_id),
        ],
        cwd=tcp.ROOT,
        capture_output=True,
        check=True,
    )
    packet = json.loads(node.stdout)
    state = base64.b64decode(packet["state_base64"])
    previous = config.get_settings
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        settings = config.Settings(
            _env_file=None,
            app_env="test",
            database_url=scoped.render_as_string(hide_password=False),
        )
        config.get_settings = lambda: settings
        command.upgrade(Config("alembic.ini"), "0008_tactics_collaboration")
        with scoped_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users(id,username,username_normalized,"
                    "display_name,password_hash,recovery_digest,credential_version,"
                    "created_at,updated_at) "
                    "VALUES(:id,'migration_user','migration_user','原昵称','test-hash',:digest,1,now(),now())"
                ),
                {"id": user_id, "digest": b"a" * 32},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO notebooks(id,kind,user_id,created_at) "
                    "VALUES(:id,'personal',:user,now())"
                ),
                {"id": notebook_id, "user": user_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO tactics(id,notebook_id,created_by,title,side,map,"
                    "status,created_at,updated_at) "
                    "VALUES(:id,:book,:user,'预置中文😀','T','mirage',"
                    "'active',now(),now())"
                ),
                {"id": tactic_id, "book": notebook_id, "user": user_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO tactic_documents(tactic_id,schema_version,epoch,"
                    "revision,yjs_state,projection,state_digest,projection_digest,updated_at) "
                    "VALUES(:id,2,:epoch,7,:state,CAST(:projection AS jsonb),"
                    ":digest,:pdigest,now())"
                ),
                {
                    "id": tactic_id,
                    "epoch": uuid.uuid4(),
                    "state": state,
                    "projection": json.dumps(packet["projection"], ensure_ascii=False),
                    "digest": bytes.fromhex(packet["state_sha256"]),
                    "pdigest": bytes.fromhex(packet["projection_sha256"]),
                },
            )
        command.upgrade(Config("alembic.ini"), "0009_tactics_teams")
        with scoped_engine.connect() as connection:
            notebook = (
                connection.execute(
                    sa.text("SELECT * FROM notebooks WHERE id=:id"), {"id": notebook_id}
                )
                .mappings()
                .one()
            )
            doc = (
                connection.execute(
                    sa.text("SELECT * FROM tactic_documents WHERE tactic_id=:id"), {"id": tactic_id}
                )
                .mappings()
                .one()
            )
            assert notebook["user_id"] == user_id and notebook["team_id"] is None
            assert doc["yjs_state"] == state and doc["revision"] == 7
            assert doc["projection"] == packet["projection"]
            assert doc["state_digest"] == bytes.fromhex(packet["state_sha256"])
    finally:
        config.get_settings = previous
        scoped_engine.dispose()
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
