"""Operator maintenance tests only in a new, identity-marked maintenance database."""

import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core import config
from app.db.tactics_models import (
    CollaborationGrant,
    CollaborationTicket,
    PersonalNotebook,
    ShareLink,
    ShareReadGrant,
    ShareVersion,
    ShareVersionItem,
    Tactic,
    TacticDocument,
    TacticsSession,
    TacticsUser,
    TacticVersion,
    Team,
    TeamInvite,
    TeamInviteAcceptance,
    TeamMember,
)
from app.services import tactics_accounts as accounts
from app.services import tactics_maintenance as maintenance
from app.services import tactics_shares as shares

PROJECT = "smokeline-tactics-maintenance-selftest"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def event_loop_policy():
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else (asyncio.DefaultEventLoopPolicy())
    )


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_MAINTENANCE_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.port == 65402
    assert parsed.database == "tactics_maintenance_selftest"
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        config.get_settings = previous
    return raw


async def control(raw, *, reopen=False):
    # Only controls the explicitly allowed NEW database; never targets another fixture.
    parsed = make_url(raw)
    assert parsed.database == "tactics_maintenance_selftest" and parsed.port == 65402
    engine = create_async_engine(
        parsed.set(database="postgres"), poolclass=NullPool, hide_parameters=True
    )
    try:
        async with engine.begin() as db:
            if reopen:
                await db.execute(
                    sa.text('ALTER DATABASE "tactics_maintenance_selftest" ALLOW_CONNECTIONS true')
                )
            row = (
                await db.execute(
                    sa.text(
                        "SELECT datallowconn,shobj_description(oid,'pg_database') "
                        "AS marker FROM pg_database WHERE datname='tactics_maintenance_selftest'"
                    )
                )
            ).one()
            return row[0], json.loads(row[1]) if row[1] else None
    finally:
        await engine.dispose()


@pytest.fixture
async def isolated(database_url, monkeypatch):
    await control(database_url, reopen=True)
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    run_id = str(uuid.uuid4())
    marker = {"kind": maintenance.KIND, "project": PROJECT, "run_id": run_id}
    monkeypatch.setenv("SMOKELINE_TACTICS_MAINTENANCE_PROJECT", PROJECT)
    async with engine.begin() as connection:
        await maintenance.write_marker(connection, "tactics_maintenance_selftest", marker)
    try:
        yield database_url, engine, factory, run_id
    finally:
        await control(database_url, reopen=True)
        await engine.dispose()


async def seed(factory):
    at = datetime.now(UTC)
    tactic_id = uuid.uuid4()
    node = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "import {createFormalSeed} from './src/formal-document.mjs';"
            "process.stdout.write(JSON.stringify(createFormalSeed({document_id:process.argv[1],"
            "title:'维护隔离正文😀',side:'T'})));",
            str(tactic_id),
        ],
        cwd=Path(__file__).resolve().parents[2] / "collaboration",
        capture_output=True,
        check=True,
    )
    packet = json.loads(node.stdout)
    async with factory() as db, db.begin():
        name = "maint_" + uuid.uuid4().hex[:18]
        user = TacticsUser(
            username=name,
            username_normalized=name,
            display_name="隔离维护用户",
            password_hash=await accounts.password_work(
                accounts.HASHER.hash, "maintenance-test-password-2026"
            ),
            recovery_digest=b"r" * 32,
        )
        db.add(user)
        await db.flush()
        book = PersonalNotebook(user_id=user.id)
        db.add(book)
        await db.flush()
        tactic = Tactic(
            id=tactic_id, notebook_id=book.id, created_by=user.id, title="维护隔离正文😀", side="T"
        )
        db.add(tactic)
        await db.flush()
        doc = TacticDocument(
            tactic_id=tactic_id,
            yjs_state=base64.b64decode(packet["state_base64"]),
            projection=packet["projection"],
            revision=7,
            state_digest=bytes.fromhex(packet["state_sha256"]),
            projection_digest=bytes.fromhex(packet["projection_sha256"]),
        )
        db.add(doc)
        cookie, csrf = accounts.add_session(db, user)
        await db.flush()
        session = await db.scalar(
            sa.select(TacticsSession).where(TacticsSession.user_id == user.id)
        )
        ticket = CollaborationTicket(
            ticket_digest=accounts.digest("ticket-" + str(tactic_id)),
            session_id=session.id,
            tactic_id=tactic_id,
            expires_at=at + timedelta(seconds=60),
        )
        db.add(ticket)
        await db.flush()
        grant = CollaborationGrant(
            ticket_id=ticket.id,
            session_id=session.id,
            tactic_id=tactic_id,
            node_instance_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
        )
        ticket.consumed_at = at
        db.add(grant)
        link = ShareLink(
            owner_notebook_id=book.id,
            tactic_id=tactic_id,
            token_digest=accounts.digest("share-" + str(tactic_id)),
            published_by=user.id,
        )
        db.add(link)
        await db.flush()
        version = await shares.publish(
            db, link, user.id, [SimpleNamespace(id=tactic_id, revision=7)], True
        )
        read = ShareReadGrant(
            grant_digest=accounts.digest("read-" + str(tactic_id)),
            share_link_id=link.id,
            expires_at=at + timedelta(minutes=5),
        )
        db.add(read)
        unused_value = secrets.token_urlsafe(32)
        unused = CollaborationTicket(
            ticket_digest=accounts.digest(unused_value),
            session_id=session.id,
            tactic_id=tactic_id,
            expires_at=at + timedelta(seconds=60),
        )
        db.add(unused)
        await db.flush()
        result = {
            "user": user.id,
            "book": book.id,
            "tactic": tactic_id,
            "session": session.id,
            "ticket": ticket.id,
            "grant": grant.id,
            "link": link.id,
            "version": version.id,
            "read": read.id,
            "cookie": cookie,
            "csrf": csrf,
            "packet": packet,
            "unused": unused.id,
            "unused_value": unused_value,
            "username": name,
        }
        return result


async def test_dry_run_and_bounded_cleanup_preserve_active_share_versions(isolated):
    raw, _, factory, run_id = isolated
    expired, active = await seed(factory), await seed(factory)
    async with factory() as db, db.begin():
        (await db.get(TacticsSession, expired["session"])).revoked_at = datetime.now(UTC)
        (await db.get(ShareReadGrant, expired["read"])).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        stored = await db.get(ShareVersion, active["version"])
        stored.created_at = datetime.now(UTC) - timedelta(days=90)
        link = await db.get(ShareLink, active["link"])
        await shares.publish(
            db, link, active["user"], [SimpleNamespace(id=active["tactic"], revision=7)], True
        )
        assert link.current_version_id != active["version"]
        (
            await db.get(
                TacticVersion,
                (
                    await db.scalar(
                        sa.select(TacticVersion.id).where(
                            TacticVersion.tactic_id == active["tactic"]
                        )
                    )
                ),
            )
        ).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    plan = await maintenance.cleanup(raw, PROJECT, run_id)
    assert plan["executed"] is False and plan["candidates"]["user_sessions"] >= 1
    async with factory() as db:
        assert await db.get(TacticsSession, expired["session"]) is not None
    result = await maintenance.cleanup(
        raw,
        PROJECT,
        run_id,
        execute=True,
        confirm_database="tactics_maintenance_selftest",
        batch_size=2,
        max_batches=50,
    )
    assert result["removed"]["user_sessions"] >= 1
    async with factory() as db:
        assert await db.get(TacticsSession, expired["session"]) is None
        assert await db.get(CollaborationGrant, expired["grant"]) is None
        assert await db.get(CollaborationTicket, expired["ticket"]) is None
        assert await db.get(TacticsSession, active["session"]) is not None
        assert await db.get(ShareVersion, active["version"]) is not None
        assert await db.get(TacticDocument, active["tactic"]) is not None
        assert await db.scalar(
            sa.select(ShareVersionItem.state).where(
                ShareVersionItem.share_version_id == active["version"]
            )
        ) == base64.b64decode(active["packet"]["state_base64"])


async def test_prepare_restore_closes_database_and_rotates_only_authority(isolated, monkeypatch):
    raw, _, factory, run_id = isolated
    data = await seed(factory)
    async with factory() as db:
        doc = await db.get(TacticDocument, data["tactic"])
        before = (
            doc.yjs_state,
            doc.projection,
            doc.state_digest,
            doc.projection_digest,
            doc.revision,
        )
        old_epoch = doc.epoch
        link = await db.get(ShareLink, data["link"])
        share_before = (
            link.token_digest,
            link.current_version_id,
            link.allow_copy,
            link.revoked_at,
        )
    dry = await maintenance.prepare_restore(raw, PROJECT, run_id)
    assert dry["executed"] is False and (await control(raw))[0] is True
    result = await maintenance.prepare_restore(
        raw, PROJECT, run_id, execute=True, confirm_database="tactics_maintenance_selftest"
    )
    allowed, marker = await control(raw)
    assert allowed is False and marker["restore_prepared"] is True
    assert marker["run_id"] == run_id and result["database_closed"] is True
    # Explicit TEST OPERATOR reopening after marker checks; never done by the utility itself.
    await control(raw, reopen=True)
    async with factory() as db:
        doc = await db.get(TacticDocument, data["tactic"])
        assert doc.epoch != old_epoch
        assert (
            doc.yjs_state,
            doc.projection,
            doc.state_digest,
            doc.projection_digest,
            doc.revision,
        ) == before
        assert (await db.get(TacticsSession, data["session"])).revoked_at is not None
        assert (await db.get(CollaborationGrant, data["grant"])).closed_at is not None
        assert (await db.get(CollaborationTicket, data["unused"])).consumed_at is not None
        assert await db.get(ShareReadGrant, data["read"]) is None
        link = await db.get(ShareLink, data["link"])
        assert (
            link.token_digest,
            link.current_version_id,
            link.allow_copy,
            link.revoked_at,
        ) == share_before
        assert (
            await db.scalar(
                sa.select(TacticVersion.source_epoch)
                .where(TacticVersion.tactic_id == data["tactic"])
                .limit(1)
            )
            == old_epoch
        )
    from fastapi import FastAPI

    from app.api import tactics_auth, tactics_collaboration
    from app.core.errors import install_error_handlers

    settings = config.Settings(
        _env_file=None,
        app_env="test",
        tactics_enabled=True,
        database_url=raw,
        public_base_url="http://maintenance.test",
        collab_internal_key="maintenance-local-internal-key-2026",
    )
    monkeypatch.setattr(accounts, "get_session_factory", lambda: factory)
    monkeypatch.setattr(tactics_auth, "get_session_factory", lambda: factory)
    monkeypatch.setattr(tactics_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(tactics_collaboration, "get_settings", lambda: settings)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(tactics_auth.router)
    app.include_router(tactics_collaboration.internal_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://maintenance.test"
    ) as client:
        client.cookies.set(accounts.COOKIE, data["cookie"])
        assert (await client.get("/api/v1/auth/session")).status_code == 401
        client.cookies.clear()
        login = await client.post(
            "/api/v1/auth/login",
            headers={"origin": settings.public_base_url},
            json={"username": data["username"], "password": "maintenance-test-password-2026"},
        )
        assert login.status_code == 200
        rejected = await client.post(
            "/api/v1/internal/tactics/tickets/consume",
            headers={"internal-key": settings.collab_internal_key.get_secret_value()},
            json={
                "ticket": data["unused_value"],
                "connection_id": str(uuid.uuid4()),
                "node_instance_id": str(uuid.uuid4()),
            },
        )
        assert rejected.status_code == 401
        rejected = await client.post(
            "/api/v1/internal/tactics/" + str(data["tactic"]) + "/load",
            headers={"internal-key": settings.collab_internal_key.get_secret_value()},
            json={"grant_id": str(data["grant"])},
        )
        assert rejected.status_code == 401


async def test_busy_and_failed_restore_preparation_stay_closed_and_rollback(isolated):
    raw, engine, factory, run_id = isolated
    data = await seed(factory)
    async with engine.connect() as extra:
        await extra.execute(sa.text("SELECT 1"))
        with pytest.raises(maintenance.MaintenanceError, match="RESTORE_TARGET_BUSY"):
            await maintenance.prepare_restore(
                raw, PROJECT, run_id, execute=True, confirm_database="tactics_maintenance_selftest"
            )
        assert (await control(raw))[0] is False
    await control(raw, reopen=True)
    async with engine.begin() as db:
        await db.execute(
            sa.text(
                "CREATE FUNCTION maintenance_test_failure() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated epoch failure'; END $$"
            )
        )
        await db.execute(
            sa.text(
                "CREATE TRIGGER maintenance_test_failure BEFORE UPDATE ON "
                "tactic_documents FOR EACH ROW EXECUTE FUNCTION maintenance_test_failure()"
            )
        )
    try:
        with pytest.raises(maintenance.MaintenanceError, match="REMAINS_CLOSED"):
            await maintenance.prepare_restore(
                raw, PROJECT, run_id, execute=True, confirm_database="tactics_maintenance_selftest"
            )
        allowed, marker = await control(raw)
        assert allowed is False and not marker.get("restore_prepared")
        await control(raw, reopen=True)
        async with factory() as db:
            assert (await db.get(TacticsSession, data["session"])).revoked_at is None
            assert (await db.get(CollaborationGrant, data["grant"])).closed_at is None
            assert await db.get(ShareReadGrant, data["read"]) is not None
    finally:
        await control(raw, reopen=True)
        async with engine.begin() as db:
            await db.execute(sa.text("DROP TRIGGER maintenance_test_failure ON tactic_documents"))
            await db.execute(sa.text("DROP FUNCTION maintenance_test_failure()"))


async def test_marker_mismatch_partial_schema_and_old_targets_fail_before_close(isolated):
    raw, engine, _, run_id = isolated
    with pytest.raises(maintenance.MaintenanceError, match="MARKER_REJECTED"):
        await maintenance.prepare_restore(
            raw,
            PROJECT,
            uuid.uuid4(),
            execute=True,
            confirm_database="tactics_maintenance_selftest",
        )
    assert (await control(raw))[0] is True
    with pytest.raises(maintenance.MaintenanceError, match="MARKER_REJECTED"):
        await maintenance.validate_empty_restore_target(raw, PROJECT, uuid.uuid4(), container=False)
    with pytest.raises(maintenance.MaintenanceError, match="NOT_EMPTY"):
        await maintenance.validate_empty_restore_target(raw, PROJECT, run_id, container=False)
    assert (await control(raw))[0] is True
    async with engine.begin() as db:
        await db.execute(sa.text("ALTER TABLE share_read_grants RENAME TO maintenance_partial"))
    try:
        with pytest.raises(maintenance.MaintenanceError, match="SCHEMA_INCOMPLETE"):
            await maintenance.prepare_restore(
                raw, PROJECT, run_id, execute=True, confirm_database="tactics_maintenance_selftest"
            )
        assert (await control(raw))[0] is True
    finally:
        async with engine.begin() as db:
            await db.execute(sa.text("ALTER TABLE maintenance_partial RENAME TO share_read_grants"))
    for name in [
        "tactics_formal_selftest",
        "tactics_shares_selftest",
        "tactics_teams_selftest",
        "smokeline",
    ]:
        old = make_url(raw).set(database=name).render_as_string(hide_password=False)
        with pytest.raises(maintenance.MaintenanceError, match="DATABASE_REJECTED"):
            maintenance.scope(old, PROJECT, run_id, execute=True, confirm_database=name)


async def test_expired_content_fk_order_and_blocked_snapshot_are_explicit(isolated):
    raw, _, factory, run_id = isolated
    expired, pinned, owner = await seed(factory), await seed(factory), await seed(factory)
    child_id = uuid.uuid4()
    generated = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "import {createFormalSeed} from './src/formal-document.mjs';"
            "process.stdout.write(JSON.stringify(createFormalSeed({document_id:process.argv[1],"
            "title:'到期队伍的活跃战术',side:'T'})));",
            str(child_id),
        ],
        cwd=Path(__file__).resolve().parents[2] / "collaboration",
        capture_output=True,
        check=True,
    )
    child = json.loads(generated.stdout)
    at = datetime.now(UTC)
    async with factory() as db, db.begin():
        for item in [expired, pinned]:
            tactic = await db.get(Tactic, item["tactic"])
            tactic.status, tactic.archived_at = "archived", at - timedelta(days=31)
        (await db.get(ShareLink, expired["link"])).revoked_at = at - timedelta(days=31)
        # Deliberate retained-snapshot pin: maintenance must not fix it with a destructive cascade.
        assert (await db.get(ShareLink, pinned["link"])).revoked_at is None
        team = Team(
            name="到期队伍",
            owner_id=owner["user"],
            status="archived",
            archived_at=at - timedelta(days=31),
        )
        db.add(team)
        await db.flush()
        db.add(TeamMember(team_id=team.id, user_id=owner["user"], role="editor"))
        book = PersonalNotebook(kind="team", user_id=None, team_id=team.id)
        db.add(book)
        await db.flush()
        db.add(
            Tactic(
                id=child_id,
                notebook_id=book.id,
                created_by=owner["user"],
                title="到期队伍的活跃战术",
                side="T",
            )
        )
        await db.flush()
        db.add(
            TacticDocument(
                tactic_id=child_id,
                yjs_state=base64.b64decode(child["state_base64"]),
                projection=child["projection"],
                state_digest=bytes.fromhex(child["state_sha256"]),
                projection_digest=bytes.fromhex(child["projection_sha256"]),
            )
        )
        invite = TeamInvite(
            team_id=team.id,
            token_digest=accounts.digest(str(uuid.uuid4())),
            created_by=owner["user"],
            created_at=at - timedelta(days=32),
            expires_at=at - timedelta(days=25),
            revoked_at=at - timedelta(days=31),
        )
        db.add(invite)
        await db.flush()
        db.add(
            TeamInviteAcceptance(
                invite_id=invite.id,
                user_id=owner["user"],
                accepted_at=at - timedelta(days=31, hours=12),
            )
        )
        team_id, book_id, invite_id = team.id, book.id, invite.id
    result = await maintenance.cleanup(
        raw,
        PROJECT,
        run_id,
        execute=True,
        confirm_database="tactics_maintenance_selftest",
        batch_size=2,
        max_batches=50,
    )
    assert result["removed"]["share_links"] >= 1 and result["removed"]["tactics"] >= 1
    assert result["remaining"]["blocked_tactics"] >= 1
    async with factory() as db:
        assert await db.get(ShareLink, expired["link"]) is None
        assert await db.get(Tactic, expired["tactic"]) is None
        assert await db.get(TacticDocument, expired["tactic"]) is None
        assert await db.get(Tactic, pinned["tactic"]) is not None
        assert await db.get(ShareVersion, pinned["version"]) is not None
        assert (
            await db.get(Team, team_id) is None and await db.get(PersonalNotebook, book_id) is None
        )
        assert (
            await db.get(Tactic, child_id) is None
            and await db.get(TacticDocument, child_id) is None
        )
        assert await db.get(TeamInvite, invite_id) is None
        assert await db.get(TacticsUser, owner["user"]) is not None
        assert await db.get(PersonalNotebook, owner["book"]) is not None


def test_project_database_user_combinations_do_not_cross(monkeypatch):
    run_id = uuid.uuid4()
    local = "postgresql+psycopg://smokeline:fixture-only@postgres:5432/smokeline"
    restored = "postgresql+psycopg://smokeline:fixture-only@postgres:5432/smokeline"
    project = "smokeline-tactics-t5-local"
    monkeypatch.setenv("SMOKELINE_TACTICS_MAINTENANCE_PROJECT", project)
    maintenance.scope(local, project, run_id, container=True)
    legacy_t5 = "postgresql+psycopg://tactics_fixture:fixture-only@postgres:5432/tactics_t5"
    with pytest.raises(maintenance.MaintenanceError, match="DATABASE_REJECTED"):
        maintenance.scope(legacy_t5, project, run_id, container=True)
    restore_project = "smokeline-restore-maintenance-test"
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", restore_project)
    maintenance.scope(restored, restore_project, run_id, container=True)
    with pytest.raises(maintenance.MaintenanceError, match="DATABASE_REJECTED"):
        maintenance.scope(legacy_t5, restore_project, run_id, container=True)
    wrong_user = local.replace("smokeline:", "tactics_fixture:")
    with pytest.raises(maintenance.MaintenanceError, match="DATABASE_REJECTED"):
        maintenance.scope(wrong_user, project, run_id, container=True)
    wrong_database = local.replace("/smokeline", "/arbitrary")
    with pytest.raises(maintenance.MaintenanceError, match="DATABASE_REJECTED"):
        maintenance.scope(wrong_database, project, run_id, container=True)


async def test_teaching_only_schema_boundary_is_legacy_compatible():
    class TeachingConnection:
        async def execute(self, query):
            assert "pg_tables" in str(query)
            return SimpleNamespace(scalars=lambda: ["targets", "lineups", "tutorial_videos"])

    assert await maintenance.formal_schema(TeachingConnection(), legacy_ok=True) is False
    with pytest.raises(maintenance.MaintenanceError, match="SCHEMA_INCOMPLETE"):
        await maintenance.formal_schema(TeachingConnection())
