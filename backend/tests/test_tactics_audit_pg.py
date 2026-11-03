"""Core real audit migration/atomicity checks, only the newly provisioned audit database."""

import base64
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
import test_tactics_collaboration as tcp
import test_tactics_teams as team_tests
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config
from app.db import tactics_models as m

event_loop_policy, live = tcp.event_loop_policy, tcp.live
register, write = team_tests.register, team_tests.write
pytestmark = pytest.mark.integration
MIGRATION_PRESERVED = False


def legacy_fixture(connection):
    """Legal 0010 rows with real Node-generated Yjs, no copied source data or actual credentials."""
    user, book, team, team_book = [uuid.uuid4() for _ in range(4)]
    ids, epochs = [uuid.uuid4() for _ in range(2)], [uuid.uuid4() for _ in range(2)]
    generated = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "import {createFormalSeed} from './src/formal-document.mjs';"
            "process.stdout.write(JSON.stringify("
            + json.dumps(
                [
                    {"document_id": str(identity), "title": f"0010迁移fixture{i}", "side": "T"}
                    for i, identity in enumerate(ids)
                ]
            )
            + ".map(createFormalSeed)));",
        ],
        cwd=tcp.ROOT,
        check=True,
        capture_output=True,
    )
    packets = json.loads(generated.stdout)
    connection.execute(
        m.TacticsUser.__table__.insert().values(
            id=user,
            username="migration_" + user.hex[:18],
            username_normalized="migration_" + user.hex[:18],
            display_name="迁移fixture",
            password_hash="public-isolated-nonlogin-placeholder",
            recovery_digest=b"0" * 32,
            credential_version=1,
        )
    )
    connection.execute(m.PersonalNotebook.__table__.insert().values(id=book, user_id=user))
    connection.execute(
        m.Team.__table__.insert().values(id=team, name="0010迁移队伍", owner_id=user)
    )
    connection.execute(
        m.TeamMember.__table__.insert().values(team_id=team, user_id=user, role="editor")
    )
    connection.execute(
        m.PersonalNotebook.__table__.insert().values(id=team_book, kind="team", team_id=team)
    )
    for identity, epoch, notebook, packet in zip(
        ids, epochs, [book, team_book], packets, strict=True
    ):
        connection.execute(
            m.Tactic.__table__.insert().values(
                id=identity,
                notebook_id=notebook,
                created_by=user,
                title=packet["projection"]["fields"]["tactic.title"],
                side="T",
            )
        )
        connection.execute(
            m.TacticDocument.__table__.insert().values(
                tactic_id=identity,
                epoch=epoch,
                yjs_state=base64.b64decode(packet["state_base64"]),
                projection=packet["projection"],
                state_digest=bytes.fromhex(packet["state_sha256"]),
                projection_digest=bytes.fromhex(packet["projection_sha256"]),
            )
        )
    packet = packets[0]
    connection.execute(
        m.TacticVersion.__table__.insert().values(
            tactic_id=ids[0],
            source_epoch=epochs[0],
            source_revision=0,
            reason="archive",
            created_by=user,
            yjs_state=base64.b64decode(packet["state_base64"]),
            projection=packet["projection"],
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    share, version, item = [uuid.uuid4() for _ in range(3)]
    connection.execute(
        m.ShareLink.__table__.insert().values(
            id=share,
            owner_notebook_id=book,
            tactic_id=ids[0],
            token_digest=b"1" * 32,
            published_by=user,
            allow_copy=False,
        )
    )
    connection.execute(
        m.ShareVersion.__table__.insert().values(
            id=version,
            share_link_id=share,
            created_by=user,
            allow_copy=False,
            manifest=[
                {
                    "item_id": str(item),
                    "title": "迁移",
                    "side": "T",
                    "map": "mirage",
                    "source_revision": 0,
                }
            ],
        )
    )
    connection.execute(
        m.ShareVersionItem.__table__.insert().values(
            share_version_id=version,
            source_tactic_id=ids[0],
            item_id=item,
            order_index=0,
            source_epoch=epochs[0],
            source_revision=0,
            projection=packet["projection"],
            state=base64.b64decode(packet["state_base64"]),
            attribution={
                "tactic_id": str(ids[0]),
                "title": "迁移",
                "author_display_name": "fixture",
            },
        )
    )
    connection.execute(
        m.ShareLink.__table__.update()
        .where(m.ShareLink.id == share)
        .values(current_version_id=version)
    )


def snapshot(connection):
    tables = [
        m.TacticsUser,
        m.PersonalNotebook,
        m.Team,
        m.TeamMember,
        m.Tactic,
        m.TacticDocument,
        m.TacticVersion,
        m.ShareLink,
        m.ShareVersion,
        m.ShareVersionItem,
    ]
    return {
        model.__tablename__: connection.execute(
            sa.select(model.__table__).order_by(*model.__table__.primary_key.columns)
        ).all()
        for model in tables
    }


@pytest.fixture(scope="module")
def database_url():
    global MIGRATION_PRESERVED
    raw = os.environ.get("TACTICS_AUDIT_TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("Explicit new tactics_audit_selftest database not arranged")
    target = make_url(raw)
    assert target.drivername == "postgresql+psycopg" and not target.query
    assert target.host == "127.0.0.1" and target.port == 65402
    assert target.username == "tactics_test" and target.database == "tactics_audit_selftest"
    engine = sa.create_engine(target, hide_parameters=True)
    previous = config.get_settings
    config.get_settings = lambda: config.Settings(_env_file=None, app_env="test", database_url=raw)
    try:
        with engine.connect() as connection:
            version = (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                if (sa.inspect(connection).has_table("alembic_version"))
                else None
            )
        if version is None:
            command.upgrade(Config("alembic.ini"), "0010_tactics_shares_history")
            with engine.begin() as connection:
                legacy_fixture(connection)
        if version in {None, "0010_tactics_shares_history"}:
            with engine.connect() as connection:
                before = snapshot(connection)
            command.upgrade(Config("alembic.ini"), "0011_tactics_audit_events")
            with engine.connect() as connection:
                assert snapshot(connection) == before
                assert (
                    connection.scalar(sa.select(sa.func.count()).select_from(m.TacticAuditEvent))
                    == 0
                )
            MIGRATION_PRESERVED = True
        else:
            assert (
                version == "0011_tactics_audit_events"
            )  # Keep existing rows, never downgrade/reset.
    finally:
        config.get_settings = previous
        engine.dispose()
    return raw


def test_0010_upgrade_preserves_real_documents_and_ownership(database_url):
    if not MIGRATION_PRESERVED:
        pytest.skip("Already 0011: preservation upgrade is not rerun or fabricated")
    assert MIGRATION_PRESERVED


async def events(factory, actor):
    async with factory() as db:
        return (
            await db.scalars(
                sa.select(m.TacticAuditEvent).where(m.TacticAuditEvent.actor_id == uuid.UUID(actor))
            )
        ).all()


async def test_audit_success_atomic_failure_idempotency_and_original_errors(live):
    backend, _, factory, _ = live
    async with httpx.AsyncClient(base_url=backend, trust_env=False) as client:
        account, headers = await register(client, "审计原子性fixture")
        actor = account["user"]["id"]
        notebook = (await client.get("/api/v1/notebooks/personal")).json()["notebook"]["id"]
        path = f"/notebooks/{notebook}/tactics"
        key = str(uuid.uuid4())
        headers["x-request-id"] = "f" * 32
        made = await write(
            client,
            path,
            headers,
            {"title": "AUDIT_BODY_SENTINEL", "side": "T"},
            key=key,
            status=201,
        )
        await write(
            client,
            path,
            headers,
            {"title": "AUDIT_BODY_SENTINEL", "side": "T"},
            key=key,
            status=201,
        )
        rows = await events(factory, actor)
        assert sorted(row.action for row in rows) == ["account.register", "tactic.create"]
        assert all(row.request_id != "f" * 32 and row.result_code == "SUCCESS" for row in rows)
        trigger = "audit_fault_" + uuid.uuid4().hex
        async with factory() as db, db.begin():
            await db.execute(
                sa.text(
                    f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'isolated audit fault'; END; $$"
                )
            )
            await db.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON tactic_audit_events "
                    f"FOR EACH ROW WHEN (NEW.actor_id='{actor}'::uuid) EXECUTE FUNCTION {trigger}()"
                )
            )
        try:
            failed = await client.post(
                "/api/v1" + path,
                headers={**headers, "idempotency-key": str(uuid.uuid4())},
                json={"title": "必须回滚", "side": "T"},
            )
            assert failed.status_code in {500, 503}
            async with factory() as db:
                assert (
                    await db.scalar(
                        sa.select(sa.func.count())
                        .select_from(m.Tactic)
                        .where(m.Tactic.notebook_id == uuid.UUID(notebook))
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        sa.select(sa.func.count())
                        .select_from(m.TacticDocument)
                        .join(m.Tactic, m.Tactic.id == m.TacticDocument.tactic_id)
                        .where(m.Tactic.notebook_id == uuid.UUID(notebook))
                    )
                    == 1
                )
            identity = made["tactic"]["id"]
            original = await write(
                client,
                f"/tactics/{identity}/archive",
                headers,
                {"expected_revision": 99},
                status=409,
            )
            assert original["error"]["code"] == "TACTIC_CONFLICT"
            await write(
                client, f"/tactics/{identity}/restore", headers, {}
            )  # Already active: no audit.
            assert len(await events(factory, actor)) == 2
        finally:
            async with factory() as db, db.begin():
                await db.execute(
                    sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON tactic_audit_events")
                )
                await db.execute(sa.text(f"DROP FUNCTION IF EXISTS {trigger}()"))
