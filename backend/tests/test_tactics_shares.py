"""Sharing/history integration restricted to the separately provisioned shares database."""

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import test_tactics_collaboration as tcp
import test_tactics_references as reference_tests
import test_tactics_teams as team_tests
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config
from app.core.errors import AppError
from app.db.tactics_models import Tactic, TacticDocument, TacticVersion
from app.services import tactics_documents as documents
from app.services import tactics_history as history
from app.services import tactics_shares as shares

event_loop_policy, live = tcp.event_loop_policy, tcp.live
register, write = team_tests.register, team_tests.write
catalog = reference_tests.catalog
test_team_permission_regression = (
    team_tests.test_team_directory_roles_invitations_transfer_archive_and_copy
)
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def spool_root(live, tmp_path):
    live[3].temp_root = str(tmp_path)


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_SHARES_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.port == 65402
    assert parsed.database == "tactics_shares_selftest"
    previous = config.get_settings
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        config.get_settings = previous
    return raw


async def public(client, path, body, *, status=200):
    result = await client.post("/api/v1/shares/" + path, json=body, headers={"origin": tcp.ORIGIN})
    assert result.status_code == status, result.text
    assert result.headers["cache-control"] == "no-store"
    return result.json()


async def test_anonymous_snapshots_paging_copy_and_history_restore(live):
    backend, _, _, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as visitor,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
    ):
        _, ah = await register(a)
        _, bh = await register(b)
        notebook = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]
        target = (await b.get("/api/v1/notebooks/personal")).json()["notebook"]
        base = {"notebook_id": notebook["id"], "tactic_id": None, "allow_copy": True}
        await write(a, "/shares", ah, {**base, "expected_revisions": []}, status=422)
        tactics = []
        for index in range(2):
            made = await write(
                a,
                f"/notebooks/{notebook['id']}/tactics",
                ah,
                {"title": f"快照{index}", "side": "T"},
                status=201,
            )
            tactics.append(made["tactic"])
        expected = [{"id": item["id"], "revision": item["revision"]} for item in tactics]
        key = str(uuid.uuid4())
        created = await write(
            a, "/shares", ah, {**base, "expected_revisions": expected}, key=key, status=201
        )
        retry = await write(a, "/shares", ah, {**base, "expected_revisions": expected}, key=key)
        assert retry["id"] == created["id"] and retry["token"] is None
        grant = (await public(visitor, "exchange", {"token": created["token"]}))["read_grant"]
        manifest = await public(visitor, "read", {"read_grant": grant})
        assert len(manifest["items"]) == 2
        assert "projection" not in str(manifest) and "notebook_id" not in manifest
        page_body = {"read_grant": grant, "version_id": manifest["version_id"], "cursor": None}
        first = await public(visitor, "items", page_body)
        second = await public(visitor, "items", {**page_body, "cursor": first["next_cursor"]})
        assert len(first["items"]) == 1 and second["next_cursor"] is None
        for page in [first, second]:
            item = page["items"][0]
            assert item["projection"]["structure"]["document_id"] == item["item_id"]
            assert item["projection"]["structure"]["document_id"] not in {
                value["id"] for value in tactics
            }
        await public(
            visitor,
            "copy",
            {"read_grant": grant, "expected_version_id": manifest["version_id"]},
            status=401,
        )
        copied = await write(
            b,
            "/shares/copy",
            bh,
            {"read_grant": grant, "expected_version_id": manifest["version_id"]},
            status=201,
        )
        assert len(copied["tactics"]) == 2
        assert all(item["notebook_id"] == target["id"] for item in copied["tactics"])
        third = await write(
            a,
            f"/notebooks/{notebook['id']}/tactics",
            ah,
            {"title": "发布后新草稿", "side": "CT"},
            status=201,
        )
        assert len((await public(visitor, "read", {"read_grant": grant}))["items"]) == 2
        await write(
            a,
            f"/shares/{created['id']}/publish",
            ah,
            {"allow_copy": True, "expected_revisions": expected},
            status=409,
        )
        published = await write(
            a,
            f"/shares/{created['id']}/publish",
            ah,
            {
                "allow_copy": False,
                "expected_revisions": [*expected, {"id": third["tactic"]["id"], "revision": 0}],
            },
        )
        assert published["version_id"] != created["version_id"]
        await public(visitor, "items", page_body, status=409)
        await write(
            b,
            "/shares/copy",
            bh,
            {"read_grant": grant, "expected_version_id": published["version_id"]},
            status=403,
        )
        versions = (await a.get(f"/api/v1/tactics/{tactics[0]['id']}/versions")).json()["items"]
        assert len(versions) == 1 and versions[0]["reason"] == "publish"
        detail = await a.get(f"/api/v1/tactics/{tactics[0]['id']}/versions/{versions[0]['id']}")
        assert detail.json()["version"] == versions[0]
        restore = await write(
            a,
            f"/tactics/{tactics[0]['id']}/versions/{versions[0]['id']}/restore-copy",
            ah,
            {"target_notebook_id": notebook["id"]},
            status=201,
        )
        assert restore["id"] != tactics[0]["id"]
        await write(a, f"/shares/{created['id']}/revoke", ah, {})
        await public(visitor, "read", {"read_grant": grant}, status=404)
        listed = (await a.get(f"/api/v1/notebooks/{notebook['id']}/shares")).json()["items"]
        assert listed[0]["revoked_at"] is not None and "token" not in listed[0]


async def test_real_websocket_draft_isolation_and_revoke_lock_order(live, database_url):
    backend, seed, _, _ = live
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-shares-smoke.mjs"],
        cwd=tcp.ROOT,
        env={
            **os.environ,
            "FORMAL_TEST_BACKEND": backend,
            "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
            "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
            "SHARES_TEST_DATABASE_URL": database_url.replace(
                "postgresql+psycopg://", "postgresql://"
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=40)
        assert process.returncode == 0, stderr.decode()
        assert b"FORMAL_SHARES_HTTP_WS_PG_PASS" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)


async def test_copy_faults_revocation_and_spool_cleanup_are_atomic(live, monkeypatch):
    backend, _, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
        httpx.AsyncClient(base_url=backend, trust_env=False) as visitor,
    ):
        _, ah = await register(a)
        _, bh = await register(b)
        source = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]
        target = (await b.get("/api/v1/notebooks/personal")).json()["notebook"]
        originals = []
        for index in range(2):
            made = await write(
                a,
                f"/notebooks/{source['id']}/tactics",
                ah,
                {"title": f"故障副本{index}", "side": "CT"},
                status=201,
            )
            originals.append({"id": made["tactic"]["id"], "revision": 0})
        created = await write(
            a,
            "/shares",
            ah,
            {
                "notebook_id": source["id"],
                "tactic_id": None,
                "allow_copy": True,
                "expected_revisions": originals,
            },
            status=201,
        )
        grant = (await public(visitor, "exchange", {"token": created["token"]}))["read_grant"]
        body = {"read_grant": grant, "expected_version_id": created["version_id"]}
        paths = []
        real_spool, real_copy = shares.spool_copies, documents.request_copy

        async def tracked(*args):
            paths.append(Path(args[2]))
            return await real_spool(*args)

        monkeypatch.setattr(shares, "spool_copies", tracked)

        async def count():
            async with factory() as db:
                return await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(Tactic)
                    .where(Tactic.notebook_id == uuid.UUID(target["id"]))
                )

        calls = 0

        async def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AppError(503, "COLLAB_UNAVAILABLE", "Injected isolated Node failure")
            return await real_copy(*args)

        monkeypatch.setattr(documents, "request_copy", fail_second)
        await write(b, "/shares/copy", bh, body, status=503)
        assert await count() == 0 and all(not path.exists() for path in paths)

        monkeypatch.setattr(documents, "request_copy", real_copy)
        manifest = await public(visitor, "read", {"read_grant": grant})
        second_title = manifest["items"][1]["title"]
        # A real database trigger rejects the second insert, after the first has been flushed.
        async with factory() as db, db.begin():
            await db.execute(
                sa.text(
                    "CREATE FUNCTION shares_test_second_insert() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN IF NEW.notebook_id='"
                    + target["id"]
                    + "'::uuid AND NEW.title='"
                    + second_title
                    + "' THEN RAISE EXCEPTION "
                    "'isolated second insert failure'; END IF; RETURN NEW; END $$"
                )
            )
            await db.execute(
                sa.text(
                    "CREATE TRIGGER shares_test_second_insert BEFORE INSERT ON "
                    "tactics FOR EACH ROW EXECUTE FUNCTION shares_test_second_insert()"
                )
            )
        try:
            await write(b, "/shares/copy", bh, body, status=500)
            assert await count() == 0 and all(not path.exists() for path in paths)
        finally:
            async with factory() as db, db.begin():
                await db.execute(sa.text("DROP TRIGGER shares_test_second_insert ON tactics"))
                await db.execute(sa.text("DROP FUNCTION shares_test_second_insert()"))
        entered, resume = asyncio.Event(), asyncio.Event()

        async def pause(*args):
            result = await real_copy(*args)
            entered.set()
            await resume.wait()
            return result

        monkeypatch.setattr(documents, "request_copy", pause)
        pending = asyncio.create_task(write(b, "/shares/copy", bh, body, status=404))
        await asyncio.wait_for(entered.wait(), 5)
        await write(b, "/shares/copy", bh, body, status=503)
        await write(a, f"/shares/{created['id']}/revoke", ah, {})
        resume.set()
        await asyncio.wait_for(pending, 5)
        assert await count() == 0 and all(not path.exists() for path in paths)


async def test_periodic_history_300_seconds_retention_and_flag_off(live, monkeypatch):
    backend, seed, factory, settings = live
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as a:
        account, headers = await register(a)
        notebook = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]
        made = await write(
            a,
            f"/notebooks/{notebook['id']}/tactics",
            headers,
            {"title": "历史检查点", "side": "T"},
            status=201,
        )
        identity = made["tactic"]["id"]
        ticket = await write(
            a, f"/tactics/{identity}/collaboration-ticket", headers, {}, status=201
        )
        process = subprocess.Popen(
            [shutil.which("node"), "scripts/formal-history-active-smoke.mjs"],
            cwd=tcp.ROOT,
            env={
                **os.environ,
                "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
                "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
                "FORMAL_TEST_TICKET": ticket["ticket"],
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5) == (
                b"ACTIVE_CHANGED\n"
            )
            async with factory() as db:
                tactic = await db.get(Tactic, uuid.UUID(identity))
                boundary = tactic.created_at
            await history.checkpoint_active(clock=lambda: boundary + timedelta(seconds=299))
            async with factory() as db:
                assert (
                    await db.scalar(
                        sa.select(sa.func.count())
                        .select_from(TacticVersion)
                        .where(TacticVersion.tactic_id == uuid.UUID(identity))
                    )
                    == 0
                )
            await history.checkpoint_active(clock=lambda: boundary + timedelta(seconds=300))
            await history.checkpoint_active(clock=lambda: boundary + timedelta(seconds=900))
            async with factory() as db, db.begin():
                versions = (
                    await db.scalars(
                        sa.select(TacticVersion).where(
                            TacticVersion.tactic_id == uuid.UUID(identity)
                        )
                    )
                ).all()
                assert len(versions) == 1 and versions[0].reason == "periodic"
                doc = await db.get(TacticDocument, uuid.UUID(identity))
                for revision in range(2, 103):
                    doc.revision = revision
                    await history.save_history(
                        db, doc, uuid.UUID(account["user"]["id"]), "periodic"
                    )
                assert (
                    await db.scalar(
                        sa.select(sa.func.count())
                        .select_from(TacticVersion)
                        .where(TacticVersion.tactic_id == uuid.UUID(identity))
                    )
                    == 100
                )
                oldest = await db.scalar(
                    sa.select(TacticVersion)
                    .where(TacticVersion.tactic_id == uuid.UUID(identity))
                    .order_by(TacticVersion.created_at, TacticVersion.id)
                    .limit(1)
                )
                oldest.expires_at = history.accounts.now() - timedelta(seconds=1)
                expired_id = oldest.id
            assert (
                await a.get(f"/api/v1/tactics/{identity}/versions/{expired_id}")
            ).status_code == 404
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)
    scans = 0

    async def forbidden_scan():
        nonlocal scans
        scans += 1
        raise AssertionError("flag-off worker must not scan")

    monkeypatch.setattr(history, "checkpoint_active", forbidden_scan)
    settings.tactics_enabled = False
    turns = 0

    async def tick(_seconds):
        nonlocal turns
        turns += 1
        if turns == 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await history.history_worker(settings, wait=tick)
    assert scans == 0


async def test_actual_main_lifespan_owns_and_cancels_history_worker(live, monkeypatch):
    backend, seed, factory, settings = live
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    spec = importlib.util.spec_from_file_location(
        "tactics_production_main_test", Path(__file__).resolve().parents[1] / "app" / "main.py"
    )
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
    settings.tactics_enabled = False
    async with production.app.router.lifespan_context(production.app):
        await asyncio.sleep(0)
        assert not any(
            task.get_coro().__qualname__ == "history_worker" for task in asyncio.all_tasks()
        )
    settings.tactics_enabled = True
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as client:
        _, headers = await register(client)
        notebook = (await client.get("/api/v1/notebooks/personal")).json()["notebook"]
        made = await write(
            client,
            f"/notebooks/{notebook['id']}/tactics",
            headers,
            {"title": "生产生命周期检查点", "side": "T"},
            status=201,
        )
        identity = made["tactic"]["id"]
        ticket = await write(
            client, f"/tactics/{identity}/collaboration-ticket", headers, {}, status=201
        )
        process = subprocess.Popen(
            [shutil.which("node"), "scripts/formal-history-active-smoke.mjs"],
            cwd=tcp.ROOT,
            env={
                **os.environ,
                "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
                "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
                "FORMAL_TEST_TICKET": ticket["ticket"],
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5) == (
                b"ACTIVE_CHANGED\n"
            )
            async with factory() as db, db.begin():
                item = await db.get(Tactic, uuid.UUID(identity))
                item.created_at = history.accounts.now() - timedelta(seconds=301)
            async with production.app.router.lifespan_context(production.app):
                await asyncio.sleep(0)
                workers = [
                    task
                    for task in asyncio.all_tasks()
                    if task.get_coro().__qualname__ == "history_worker"
                ]
                assert len(workers) == 1 and not workers[0].done()
                deadline = asyncio.get_running_loop().time() + 20
                while True:
                    async with factory() as db:
                        saved = await db.scalar(
                            sa.select(TacticVersion).where(
                                TacticVersion.tactic_id == uuid.UUID(identity)
                            )
                        )
                    if saved is not None:
                        assert saved.reason == "periodic" and saved.source_revision == 1
                        break
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.2)
            assert workers[0].cancelled()
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


async def test_whole_copy_quota_insufficient_space_and_invalid_public_credentials(
    live, monkeypatch
):
    backend, _, factory, settings = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as a,
        httpx.AsyncClient(base_url=backend, trust_env=False) as b,
        httpx.AsyncClient(base_url=backend, trust_env=False) as visitor,
    ):
        _, ah = await register(a)
        receiver, bh = await register(b)
        source = (await a.get("/api/v1/notebooks/personal")).json()["notebook"]
        target = (await b.get("/api/v1/notebooks/personal")).json()["notebook"]
        expected = []
        for _ in range(2):
            item = await write(
                a,
                f"/notebooks/{source['id']}/tactics",
                ah,
                {"title": "整本配额", "side": "T"},
                status=201,
            )
            expected.append({"id": item["tactic"]["id"], "revision": 0})
        created = await write(
            a,
            "/shares",
            ah,
            {
                "notebook_id": source["id"],
                "tactic_id": None,
                "allow_copy": True,
                "expected_revisions": expected,
            },
            status=201,
        )
        await public(visitor, "exchange", {"token": "bad"}, status=404)
        await public(visitor, "read", {"read_grant": "bad"}, status=404)
        wrong_origin = await visitor.post(
            "/api/v1/shares/exchange",
            json={"token": created["token"]},
            headers={"origin": "http://wrong.test"},
        )
        assert (
            wrong_origin.status_code == 403 and wrong_origin.headers["cache-control"] == "no-store"
        )
        visitor.cookies.set(history.accounts.COOKIE, "invalid-cookie")
        grant = (await public(visitor, "exchange", {"token": created["token"]}))["read_grant"]
        body = {"read_grant": grant, "expected_version_id": created["version_id"]}
        real_disk = shares.shutil.disk_usage
        from types import SimpleNamespace

        monkeypatch.setattr(shares.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
        await write(b, "/shares/copy", bh, body, status=503)
        assert not list(Path(settings.temp_root).glob("smokeline-share-copy-*"))
        monkeypatch.setattr(shares.shutil, "disk_usage", real_disk)
        # Legal isolated persisted documents generated by the actual Node seed implementation.
        generated = subprocess.run(
            [
                shutil.which("node"),
                "--input-type=module",
                "-e",
                "import {randomUUID} from 'node:crypto';"
                "import {createFormalSeed} from './src/formal-document.mjs';"
                "process.stdout.write(JSON.stringify(Array.from({length:99},()=>"
                "createFormalSeed({document_id:randomUUID(),title:'配额fixture',side:'T'}))));",
            ],
            cwd=tcp.ROOT,
            capture_output=True,
            check=True,
        )
        async with factory() as db, db.begin():
            for packet in json.loads(generated.stdout):
                identity = uuid.UUID(packet["projection"]["structure"]["document_id"])
                db.add(
                    Tactic(
                        id=identity,
                        notebook_id=uuid.UUID(target["id"]),
                        created_by=uuid.UUID(receiver["user"]["id"]),
                        title="配额fixture",
                        side="T",
                    )
                )
                await db.flush()
                db.add(
                    TacticDocument(
                        tactic_id=identity,
                        yjs_state=base64.b64decode(packet["state_base64"]),
                        projection=packet["projection"],
                        state_digest=bytes.fromhex(packet["state_sha256"]),
                        projection_digest=bytes.fromhex(packet["projection_sha256"]),
                    )
                )
                await db.flush()
        await write(b, "/shares/copy", bh, body, status=409)
        async with factory() as db:
            assert (
                await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(Tactic)
                    .where(Tactic.notebook_id == uuid.UUID(target["id"]))
                )
                == 99
            )


def test_0009_upgrade_preserves_documents_and_rejects_inconsistent_history(database_url):
    schema = "t4_migration_" + uuid.uuid4().hex
    engine = sa.create_engine(database_url)
    scoped = make_url(database_url).update_query_dict({"options": f"-csearch_path={schema}"})
    isolated = sa.create_engine(scoped)
    user_id, notebook_id, tactic_id, epoch = [uuid.uuid4() for _ in range(4)]
    generated = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "import {createFormalSeed} from './src/formal-document.mjs';"
            "process.stdout.write(JSON.stringify(['原文','冲突原文'].map(title=>"
            "createFormalSeed({document_id:process.argv[1],title,side:'T'}))));",
            str(tactic_id),
        ],
        cwd=tcp.ROOT,
        capture_output=True,
        check=True,
    )
    original, conflicting = json.loads(generated.stdout)
    previous = config.get_settings
    try:
        with engine.begin() as db:
            db.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        settings = config.Settings(
            _env_file=None,
            app_env="test",
            database_url=scoped.render_as_string(hide_password=False),
        )
        config.get_settings = lambda: settings
        command.upgrade(Config("alembic.ini"), "0009_tactics_teams")
        with isolated.begin() as db:
            db.execute(
                sa.text(
                    "INSERT INTO users(id,username,username_normalized,display_name,"
                    "password_hash,recovery_digest,credential_version,created_at,updated_at) "
                    "VALUES(:id,'migration','migration','原昵称','fixture',:digest,1,now(),now())"
                ),
                {"id": user_id, "digest": b"b" * 32},
            )
            db.execute(
                sa.text(
                    "INSERT INTO notebooks(id,kind,user_id,created_at) "
                    "VALUES(:id,'personal',:user,now())"
                ),
                {"id": notebook_id, "user": user_id},
            )
            db.execute(
                sa.text(
                    "INSERT INTO tactics(id,notebook_id,created_by,title,side,map,status,"
                    "created_at,updated_at) "
                    "VALUES(:id,:book,:user,'原文','T','mirage','active',now(),now())"
                ),
                {"id": tactic_id, "book": notebook_id, "user": user_id},
            )
            db.execute(
                sa.text(
                    "INSERT INTO tactic_documents(tactic_id,schema_version,epoch,revision,"
                    "yjs_state,projection,state_digest,projection_digest,updated_at) "
                    "VALUES(:id,2,:epoch,7,:state,CAST(:p AS jsonb),:digest,:pdigest,now())"
                ),
                {
                    "id": tactic_id,
                    "epoch": epoch,
                    "state": base64.b64decode(original["state_base64"]),
                    "p": json.dumps(original["projection"], ensure_ascii=False),
                    "digest": bytes.fromhex(original["state_sha256"]),
                    "pdigest": bytes.fromhex(original["projection_sha256"]),
                },
            )
            duplicate_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
            for identity, packet in zip(
                duplicate_ids, [original, original, conflicting], strict=True
            ):
                db.execute(
                    sa.text(
                        "INSERT INTO tactic_versions(id,tactic_id,source_epoch,"
                        "source_revision,reason,yjs_state,projection,created_by,"
                        "created_at,expires_at) "
                        "VALUES(:id,:tactic,:epoch,7,'archive',:state,CAST(:p AS jsonb),:user,"
                        "now(),now()+interval '30 days')"
                    ),
                    {
                        "id": identity,
                        "tactic": tactic_id,
                        "epoch": epoch,
                        "user": user_id,
                        "state": base64.b64decode(packet["state_base64"]),
                        "p": json.dumps(packet["projection"], ensure_ascii=False),
                    },
                )
        with pytest.raises(sa.exc.DBAPIError):
            command.upgrade(Config("alembic.ini"), "0010_tactics_shares_history")
        with isolated.begin() as db:
            assert db.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                "0009_tactics_teams"
            )
            assert db.scalar(sa.text("SELECT count(*) FROM tactic_versions")) == 3
            db.execute(
                sa.text("DELETE FROM tactic_versions WHERE id=:id"), {"id": duplicate_ids[2]}
            )
        command.upgrade(Config("alembic.ini"), "0010_tactics_shares_history")
        with isolated.connect() as db:
            assert db.scalar(sa.text("SELECT count(*) FROM tactic_versions")) == 1
            row = db.execute(sa.text("SELECT * FROM tactic_documents")).mappings().one()
            assert row["tactic_id"] == tactic_id and row["epoch"] == epoch and row["revision"] == 7
            assert row["yjs_state"] == base64.b64decode(original["state_base64"])
            assert row["projection"] == original["projection"]
            assert db.scalar(sa.text("SELECT id FROM notebooks")) == notebook_id
    finally:
        config.get_settings = previous
        isolated.dispose()
        with engine.begin() as db:
            db.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


async def test_shared_teaching_only_resolves_snapshot_references_and_public_rows(live, catalog):
    backend, seed, factory, _ = live
    async with (
        httpx.AsyncClient(base_url=backend, trust_env=False) as owner,
        httpx.AsyncClient(base_url=backend, trust_env=False) as visitor,
    ):
        _, headers = await register(owner)
        notebook = (await owner.get("/api/v1/notebooks/personal")).json()["notebook"]
        made = await write(
            owner,
            f"/notebooks/{notebook['id']}/tactics",
            headers,
            {"title": "分享教学", "side": "T"},
            status=201,
        )
        identity = made["tactic"]["id"]
        ticket = await write(
            owner, f"/tactics/{identity}/collaboration-ticket", headers, {}, status=201
        )
        process = subprocess.Popen(
            [shutil.which("node"), "scripts/formal-history-active-smoke.mjs"],
            cwd=tcp.ROOT,
            env={
                **os.environ,
                "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
                "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
                "FORMAL_TEST_TICKET": ticket["ticket"],
                "FORMAL_HISTORY_LINEUPS": json.dumps([catalog["visible"]]),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5) == (
                b"ACTIVE_CHANGED\n"
            )
            created = await write(
                owner,
                "/shares",
                headers,
                {
                    "notebook_id": notebook["id"],
                    "tactic_id": identity,
                    "allow_copy": False,
                    "expected_revisions": [{"id": identity, "revision": 1}],
                },
                status=201,
            )
            grant = (await public(visitor, "exchange", {"token": created["token"]}))["read_grant"]
            manifest = await public(visitor, "read", {"read_grant": grant})
            body = {
                "read_grant": grant,
                "version_id": created["version_id"],
                "item_id": manifest["items"][0]["item_id"],
                "lineup_id": catalog["visible"],
            }
            available = await public(visitor, "lineups", body)
            assert (
                available["available"] is True and available["lineup"]["id"] == catalog["visible"]
            )
            await public(visitor, "lineups", {**body, "lineup_id": catalog["hidden"]}, status=404)
            await public(visitor, "lineups", {**body, "item_id": identity}, status=404)
            await public(visitor, "lineups", {**body, "version_id": str(uuid.uuid4())}, status=409)
            async with factory() as db, db.begin():
                from app.db.models import TutorialVideo, VideoStatus

                await db.execute(
                    sa.update(TutorialVideo)
                    .where(TutorialVideo.lineup_id == uuid.UUID(catalog["visible"]))
                    .values(status=VideoStatus.archived)
                )
            assert await public(visitor, "lineups", body) == {
                "id": catalog["visible"],
                "available": False,
                "lineup": None,
            }
            async with factory() as db, db.begin():
                await db.execute(
                    sa.text("ALTER TABLE tutorial_videos RENAME TO shares_video_fault")
                )
            try:
                result = await public(visitor, "lineups", body, status=500)
                assert "available" not in result
            finally:
                async with factory() as db, db.begin():
                    await db.execute(
                        sa.text("ALTER TABLE shares_video_fault RENAME TO tutorial_videos")
                    )
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)
