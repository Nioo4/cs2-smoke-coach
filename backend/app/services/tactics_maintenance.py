"""Explicitly isolated maintenance; no application route or implicit production settings."""

import json
import os
import re
import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

FORMAL_TABLES = {
    "users",
    "user_sessions",
    "notebooks",
    "tactics",
    "tactic_documents",
    "tactic_versions",
    "api_idempotency",
    "collaboration_tickets",
    "collaboration_grants",
    "tactic_operations",
    "tactic_recovery_items",
    "teams",
    "team_members",
    "team_invites",
    "team_invite_acceptances",
    "share_links",
    "share_versions",
    "share_version_items",
    "share_read_grants",
}
PROJECT = re.compile(
    r"^smokeline-(?:restore|tactics-maintenance|tactics-t5)-[a-z0-9][a-z0-9-]{0,40}$"
)
KIND = "smokeline-tactics-isolated-v1"
LOCK_KEY = 1397575501


class MaintenanceError(RuntimeError):
    """Only fixed safe codes leave the CLI; never stringify SQL/connection exceptions."""


def scope(raw, project, run_id, *, execute=False, confirm_database=None, container=False):
    try:
        target = make_url(raw)
        marker = (
            "SMOKELINE_RESTORE_PROJECT"
            if project.startswith("smokeline-restore-")
            else ("SMOKELINE_TACTICS_MAINTENANCE_PROJECT")
        )
        if not PROJECT.fullmatch(project) or os.environ.get(marker) != project:
            raise MaintenanceError("MAINTENANCE_PROJECT_REJECTED")
        local = (
            project.startswith("smokeline-tactics-maintenance-")
            and target.username == "tactics_test"
            and target.host == "127.0.0.1"
            and target.port == 65402
            and (target.database == "tactics_maintenance_selftest")
        )
        internal_identity = (
            project == "smokeline-tactics-t5-local"
            and target.database == "smokeline"
            and target.username == "smokeline"
        ) or (
            project.startswith("smokeline-restore-")
            and target.database == "smokeline"
            and target.username == "smokeline"
        )
        internal = (
            container
            and target.host == "postgres"
            and target.port in {None, 5432}
            and (internal_identity)
        )
        if (
            target.drivername not in {"postgresql", "postgresql+psycopg"}
            or target.query
            or (not (local or internal) or not target.password)
        ):
            raise MaintenanceError("MAINTENANCE_DATABASE_REJECTED")
        if execute and confirm_database != target.database:
            raise MaintenanceError("MAINTENANCE_CONFIRM_DATABASE_REQUIRED")
        return target.set(drivername="postgresql+psycopg"), str(uuid.UUID(str(run_id)))
    except (ValueError, TypeError, sa.exc.ArgumentError):
        raise MaintenanceError("MAINTENANCE_SCOPE_REJECTED") from None


async def formal_schema(connection, *, legacy_ok=False):
    names = set(
        (
            await connection.execute(
                sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            )
        ).scalars()
    )
    audit = "tactic_audit_events"
    if not names & (FORMAL_TABLES | {audit}) and legacy_ok:
        return False
    if not FORMAL_TABLES <= names:
        raise MaintenanceError("MAINTENANCE_FORMAL_SCHEMA_INCOMPLETE")
    version = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    if await connection.scalar(sa.text("SELECT current_schema()")) != "public" or version not in {
        "0010_tactics_shares_history", "0011_tactics_audit_events"
    } or (audit in names) != (version == "0011_tactics_audit_events"):
        raise MaintenanceError("MAINTENANCE_SCHEMA_REJECTED")
    if version == "0011_tactics_audit_events":
        columns = set((await connection.execute(sa.text(
            "SELECT column_name,data_type,is_nullable,character_maximum_length "
            "FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='tactic_audit_events'"))).all())
        expected = {(name, "uuid", "YES", None) for name in (
            "actor_id", "tactic_id", "team_id", "resource_id")}
        expected |= {("id", "uuid", "NO", None),
                     ("created_at", "timestamp with time zone", "NO", None),
                     ("action", "character varying", "NO", 64),
                     ("result_code", "character varying", "NO", 64),
                     ("request_id", "character varying", "NO", 32)}
        constraints = (await connection.execute(sa.text(
            "SELECT contype,conname,confdeltype FROM pg_constraint "
            "WHERE conrelid='public.tactic_audit_events'::regclass"))).all()
        checks = {row[1] for row in constraints if row[0] == "c"}
        indexes = await connection.scalar(sa.text(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='tactic_audit_events' AND indexname IN "
            "('ix_tactic_audit_time','ix_tactic_audit_tactic','ix_tactic_audit_team')"))
        if columns != expected or not {
            "ck_tactic_audit_action", "ck_tactic_audit_result", "ck_tactic_audit_request"
        } <= checks or sum(row[0] == "p" for row in constraints) != 1 or sum(
            row[0] == "f" and row[2] == "n" for row in constraints
        ) != 3 or indexes != 3:
            raise MaintenanceError("MAINTENANCE_AUDIT_SCHEMA_REJECTED")
    return version


async def identity(connection, project, run_id, expected_database):
    if await connection.scalar(sa.text("SELECT current_database()")) != expected_database:
        raise MaintenanceError("MAINTENANCE_DATABASE_IDENTITY_REJECTED")
    raw = await connection.scalar(
        sa.text(
            "SELECT shobj_description(oid,'pg_database') "
            "FROM pg_database WHERE datname=current_database()"
        )
    )
    try:
        marker = json.loads(raw)
        if (
            marker.get("kind") != KIND
            or marker.get("project") != project
            or (marker.get("run_id") != str(run_id))
        ):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise MaintenanceError("MAINTENANCE_DATABASE_MARKER_REJECTED") from None
    return marker


def _database_name(name):
    return postgresql.dialect().identifier_preparer.quote_identifier(name)


async def write_marker(connection, database, marker):
    literal = sa.literal(json.dumps(marker, sort_keys=True)).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    await connection.execute(
        sa.text(f"COMMENT ON DATABASE {_database_name(database)} IS {literal}")
    )


async def validate_empty_restore_target(raw, project, run_id, *, container=True):
    target, run_id = scope(
        raw,
        project,
        run_id,
        execute=True,
        confirm_database=make_url(raw).database,
        container=container,
    )
    engine = create_async_engine(target, hide_parameters=True, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            await identity(connection, project, run_id, target.database)
            if await connection.scalar(
                sa.text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            ):
                raise MaintenanceError("RESTORE_TARGET_DATABASE_NOT_EMPTY")
            if await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND pid<>pg_backend_pid()"
                )
            ):
                raise MaintenanceError("RESTORE_TARGET_HAS_CONNECTIONS")
    except MaintenanceError:
        raise
    except Exception:
        raise MaintenanceError("RESTORE_TARGET_PREFLIGHT_FAILED") from None
    finally:
        await engine.dispose()


async def prepare_restore(
    raw, project, run_id, *, execute=False, confirm_database=None, container=False, legacy_ok=False
):
    # The legacy bridge may discover a teaching-only backup before a new run UUID is required.
    candidate_run = run_id or uuid.UUID(int=0)
    target, normalized = scope(
        raw,
        project,
        candidate_run,
        execute=execute,
        confirm_database=confirm_database,
        container=container,
    )
    engine = create_async_engine(target, hide_parameters=True, poolclass=NullPool)
    closed = False
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                if not await formal_schema(connection, legacy_ok=legacy_ok):
                    return {"formal": False, "executed": False}
                if run_id is None:
                    raise MaintenanceError("RESTORE_RUN_ID_REQUIRED")
                marker = await identity(connection, project, normalized, target.database)
                counts = {}
                for table in [
                    "user_sessions",
                    "collaboration_tickets",
                    "collaboration_grants",
                    "share_read_grants",
                    "tactic_documents",
                ]:
                    counts[table] = await connection.scalar(
                        sa.text(f"SELECT count(*) FROM {table}")
                    )
            if not execute:
                return {"formal": True, "executed": False, "counts": counts}
            # This connection already exists. Committing the gate stops ALL future connections.
            control_engine = create_async_engine(
                target.set(database="postgres"), hide_parameters=True, poolclass=NullPool
            )
            try:
                async with control_engine.begin() as control:
                    raw_marker = await control.scalar(
                        sa.text(
                            "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                            "WHERE datname=:database"
                        ),
                        {"database": target.database},
                    )
                    if json.loads(raw_marker) != marker:
                        raise MaintenanceError("MAINTENANCE_DATABASE_MARKER_REJECTED")
                    marker = {
                        key: value
                        for key, value in marker.items()
                        if key not in {"restore_prepared", "prepared_at", "prepared_counts"}
                    }
                    marker["restore_prepared"] = False
                    await write_marker(control, target.database, marker)
                    await control.execute(
                        sa.text(
                            f"ALTER DATABASE {_database_name(target.database)} "
                            "ALLOW_CONNECTIONS false"
                        )
                    )
                closed = True
            finally:
                await control_engine.dispose()
            async with connection.begin():
                others = await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid()"
                    )
                )
                if others:
                    raise MaintenanceError("RESTORE_TARGET_BUSY_DATABASE_REMAINS_CLOSED")
                await connection.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY}
                )
                at = await connection.scalar(sa.text("SELECT CURRENT_TIMESTAMP"))
                queries = {
                    "sessions_revoked": "UPDATE user_sessions SET revoked_at=:at "
                                        "WHERE revoked_at IS NULL",
                    "tickets_consumed": "UPDATE collaboration_tickets SET consumed_at=:at "
                                        "WHERE consumed_at IS NULL",
                    "grants_closed": "UPDATE collaboration_grants SET closed_at=:at "
                                     "WHERE closed_at IS NULL",
                    "read_grants_deleted": "DELETE FROM share_read_grants",
                    "documents_rotated": "UPDATE tactic_documents SET epoch=gen_random_uuid(),"
                                         "updated_at=:at",
                }
                changed = {}
                for label, query in queries.items():
                    result = await connection.execute(sa.text(query), {"at": at})
                    changed[label] = result.rowcount
                marker = {
                    **marker,
                    "restore_prepared": True,
                    "prepared_at": at.isoformat(),
                    "prepared_counts": changed,
                }
                await write_marker(connection, target.database, marker)
            return {
                "formal": True,
                "executed": True,
                "database_closed": True,
                "project": project,
                "run_id": normalized,
                "counts": changed,
                "reopen_instruction": "From the postgres control connection, verify this new "
                "database's datallowconn=false and COMMENT project/run/prepared; stop old "
                "API/Node, then explicitly ALTER DATABASE "
                + _database_name(target.database)
                + " ALLOW_CONNECTIONS true before starting fresh services.",
            }
    except MaintenanceError:
        raise
    except Exception:
        raise MaintenanceError(
            "RESTORE_PREPARE_FAILED_DATABASE_REMAINS_CLOSED"
            if closed
            else "RESTORE_PREPARE_FAILED_NO_OPEN_MARKER"
        ) from None
    finally:
        await engine.dispose()


AUTH = {
    "collaboration_grants": "x.closed_at IS NOT NULL OR s.revoked_at IS NOT NULL "
                            "OR s.expires_at<=:at",
    "collaboration_tickets": "(x.consumed_at IS NOT NULL OR x.expires_at<=:at OR "
    "s.revoked_at IS NOT NULL OR s.expires_at<=:at) AND NOT EXISTS "
    "(SELECT 1 FROM collaboration_grants g WHERE g.ticket_id=x.id)",
    "user_sessions": "(x.revoked_at IS NOT NULL OR x.expires_at<=:at) AND NOT EXISTS "
    "(SELECT 1 FROM collaboration_grants g WHERE g.session_id=x.id) AND NOT EXISTS "
    "(SELECT 1 FROM collaboration_tickets t WHERE t.session_id=x.id)",
}
LEAF = {
    "share_read_grants": "expires_at<=:at",
    "api_idempotency": "expires_at<=:at",
    "tactic_recovery_items": "expires_at<=:at",
    "tactic_versions": "expires_at<=:at OR id IN (SELECT id FROM (SELECT id,row_number() "
    "OVER(PARTITION BY tactic_id ORDER BY created_at DESC,id DESC) n "
    "FROM tactic_versions) ranked WHERE n>100)",
}
PINNED = "EXISTS(SELECT 1 FROM share_version_items i WHERE i.source_tactic_id=t.id) OR "
PINNED += "EXISTS(SELECT 1 FROM share_links l WHERE l.tactic_id=t.id)"


async def _domain(connection, notebook_id):
    team = await connection.scalar(
        sa.text("SELECT team_id FROM notebooks WHERE id=:id"), {"id": notebook_id}
    )
    if team:
        await connection.execute(
            sa.text("SELECT id FROM teams WHERE id=:id FOR UPDATE"), {"id": team}
        )
    await connection.execute(
        sa.text("SELECT id FROM notebooks WHERE id=:id FOR UPDATE"), {"id": notebook_id}
    )


async def _delete_ids(connection, table, ids):
    if not ids:
        return 0
    return (
        await connection.execute(sa.text(f"DELETE FROM {table} WHERE id=ANY(:ids)"), {"ids": ids})
    ).rowcount


async def _auth_one(connection, table, identity_, at):
    sid = (
        identity_
        if table == "user_sessions"
        else await connection.scalar(
            sa.text(f"SELECT session_id FROM {table} WHERE id=:id"), {"id": identity_}
        )
    )
    if sid is None:
        return 0
    user = await connection.scalar(
        sa.text("SELECT user_id FROM user_sessions WHERE id=:id"), {"id": sid}
    )
    if (
        user is None
        or await connection.scalar(
            sa.text("SELECT id FROM users WHERE id=:id FOR NO KEY UPDATE SKIP LOCKED"), {"id": user}
        )
        is None
    ):
        return 0
    if (
        await connection.scalar(
            sa.text("SELECT id FROM user_sessions WHERE id=:id FOR UPDATE SKIP LOCKED"), {"id": sid}
        )
        is None
    ):
        return 0
    join = "" if table == "user_sessions" else "JOIN user_sessions s ON s.id=x.session_id"
    eligible = await connection.scalar(
        sa.text(
            f"SELECT x.id FROM {table} x {join} "
            f"WHERE x.id=:id AND ({AUTH[table]}) FOR UPDATE OF x SKIP LOCKED"
        ),
        {"id": identity_, "at": at},
    )
    return await _delete_ids(connection, table, [eligible]) if eligible else 0


async def _purge_link(connection, identity_, cutoff):
    book = await connection.scalar(
        sa.text("SELECT owner_notebook_id FROM share_links WHERE id=:id"), {"id": identity_}
    )
    if book is None:
        return 0
    await _domain(connection, book)
    eligible = await connection.scalar(
        sa.text("SELECT id FROM share_links WHERE id=:id AND revoked_at<=:cutoff FOR UPDATE"),
        {"id": identity_, "cutoff": cutoff},
    )
    if not eligible:
        return 0
    await connection.execute(
        sa.text("UPDATE share_links SET current_version_id=NULL WHERE id=:id"), {"id": identity_}
    )
    await connection.execute(
        sa.text("DELETE FROM share_read_grants WHERE share_link_id=:id"), {"id": identity_}
    )
    await connection.execute(
        sa.text(
            "DELETE FROM share_version_items WHERE share_version_id IN "
            "(SELECT id FROM share_versions WHERE share_link_id=:id)"
        ),
        {"id": identity_},
    )
    await connection.execute(
        sa.text("DELETE FROM share_versions WHERE share_link_id=:id"), {"id": identity_}
    )
    return await _delete_ids(connection, "share_links", [identity_])


async def _purge_tactic(connection, identity_):
    pinned = await connection.scalar(
        sa.text(f"SELECT ({PINNED}) FROM tactics t WHERE t.id=:id"), {"id": identity_}
    )
    if pinned is None or pinned:
        return 0
    await connection.execute(
        sa.text("SELECT id FROM tactics WHERE id=:id FOR UPDATE"), {"id": identity_}
    )
    await connection.execute(
        sa.text("SELECT tactic_id FROM tactic_documents WHERE tactic_id=:id FOR UPDATE"),
        {"id": identity_},
    )
    for table in [
        "collaboration_grants",
        "collaboration_tickets",
        "tactic_operations",
        "tactic_recovery_items",
        "tactic_versions",
        "tactic_documents",
    ]:
        await connection.execute(
            sa.text(f"DELETE FROM {table} WHERE tactic_id=:id"), {"id": identity_}
        )
    return await _delete_ids(connection, "tactics", [identity_])


async def _content_one(connection, kind, identity_, cutoff):
    if kind == "share_links":
        return await _purge_link(connection, identity_, cutoff)
    if kind == "tactics":
        book = await connection.scalar(
            sa.text("SELECT notebook_id FROM tactics WHERE id=:id"), {"id": identity_}
        )
        if book is None:
            return 0
        await _domain(connection, book)
        if not await connection.scalar(
            sa.text(
                "SELECT id FROM tactics WHERE id=:id AND status='archived' AND archived_at<=:cutoff"
            ),
            {"id": identity_, "cutoff": cutoff},
        ):
            return 0
        return await _purge_tactic(connection, identity_)
    team = await connection.scalar(
        sa.text(
            "SELECT id FROM teams WHERE id=:id AND status='archived' "
            "AND archived_at<=:cutoff FOR UPDATE"
        ),
        {"id": identity_, "cutoff": cutoff},
    )
    if not team:
        return 0
    book = await connection.scalar(
        sa.text("SELECT id FROM notebooks WHERE team_id=:id FOR UPDATE"), {"id": identity_}
    )
    blocked = await connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM share_links "
            "WHERE owner_notebook_id=:book) OR EXISTS(SELECT 1 FROM tactics t WHERE "
            f"t.notebook_id=:book AND ({PINNED}))"
        ),
        {"book": book},
    )
    if blocked:
        return 0
    ids = (
        (
            await connection.execute(
                sa.text("SELECT id FROM tactics WHERE notebook_id=:id ORDER BY id"), {"id": book}
            )
        )
        .scalars()
        .all()
    )
    for tactic_id in ids:
        await _purge_tactic(connection, tactic_id)
    await _delete_ids(connection, "notebooks", [book])
    await connection.execute(
        sa.text(
            "DELETE FROM team_invite_acceptances WHERE invite_id IN "
            "(SELECT id FROM team_invites WHERE team_id=:id)"
        ),
        {"id": identity_},
    )
    await connection.execute(
        sa.text("DELETE FROM team_invites WHERE team_id=:id"), {"id": identity_}
    )
    await connection.execute(
        sa.text("DELETE FROM team_members WHERE team_id=:id"), {"id": identity_}
    )
    return await _delete_ids(connection, "teams", [identity_])


def audit_leaves(enabled):
    return ({"tactic_audit_events": "created_at<=:at - INTERVAL '30 days'", **LEAF}
            if enabled else LEAF)


async def cleanup_plan(connection, at, *, with_audit=False):
    counts = {}
    for table, predicate in AUTH.items():
        join = "" if table == "user_sessions" else "JOIN user_sessions s ON s.id=x.session_id"
        terminal = predicate.split(" AND NOT EXISTS", 1)[0]
        counts[table] = await connection.scalar(
            sa.text(f"SELECT count(*) FROM {table} x {join} WHERE {terminal}"), {"at": at}
        )
        counts[f"blocked_{table}"] = await connection.scalar(
            sa.text(
                f"SELECT count(*) FROM {table} x {join} WHERE ({terminal}) AND NOT ({predicate})"
            ),
            {"at": at},
        )
    leaf = audit_leaves(with_audit)
    for table, predicate in leaf.items():
        counts[table] = await connection.scalar(
            sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}"), {"at": at}
        )
    cutoff = at - timedelta(days=30)
    for table in ["share_links", "tactics", "teams"]:
        predicate = (
            "revoked_at<=:cutoff"
            if table == "share_links"
            else ("status='archived' AND archived_at<=:cutoff")
        )
        counts[table] = await connection.scalar(
            sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}"), {"cutoff": cutoff}
        )
    counts["blocked_tactics"] = await connection.scalar(
        sa.text(
            "SELECT count(*) FROM tactics t "
            f"WHERE t.status='archived' AND t.archived_at<=:cutoff AND ({PINNED})"
        ),
        {"cutoff": cutoff},
    )
    counts["blocked_teams"] = await connection.scalar(
        sa.text(
            "SELECT count(*) FROM teams tm "
            "JOIN notebooks n ON n.team_id=tm.id WHERE tm.status='archived' "
            "AND tm.archived_at<=:cutoff "
            "AND (EXISTS(SELECT 1 FROM share_links l WHERE l.owner_notebook_id=n.id) OR EXISTS("
            f"SELECT 1 FROM tactics t WHERE t.notebook_id=n.id AND ({PINNED})))"
        ),
        {"cutoff": cutoff},
    )
    return counts


async def cleanup(
    raw,
    project,
    run_id,
    *,
    execute=False,
    confirm_database=None,
    container=False,
    batch_size=100,
    max_batches=10,
):
    if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 100:
        raise MaintenanceError("MAINTENANCE_BATCH_REJECTED")
    target, run_id = scope(
        raw,
        project,
        run_id,
        execute=execute,
        confirm_database=confirm_database,
        container=container,
    )
    engine = create_async_engine(target, hide_parameters=True, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                if not execute:
                    await connection.execute(
                        sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                await identity(connection, project, run_id, target.database)
                schema = await formal_schema(connection)
                with_audit = schema == "0011_tactics_audit_events"
                leaf = audit_leaves(with_audit)
                at = await connection.scalar(sa.text("SELECT CURRENT_TIMESTAMP"))
                before = await cleanup_plan(connection, at, with_audit=with_audit)
            if not execute:
                return {
                    "executed": False,
                    "project": project,
                    "run_id": run_id,
                    "cutoff": at.isoformat(),
                    "candidates": before,
                }
            locked = await connection.scalar(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            )
            if not locked:
                raise MaintenanceError("MAINTENANCE_ALREADY_RUNNING")
            # Check the session lock result before beginning bounded resource transactions.
            await connection.commit()
            removed = {key: 0 for key in before if not key.startswith("blocked_")}
            cutoff = at - timedelta(days=30)
            for table in [*AUTH, *leaf, "share_links", "tactics", "teams"]:
                after_id = None
                for _ in range(max_batches):
                    async with connection.begin():
                        await identity(connection, project, run_id, target.database)
                        if table in AUTH:
                            join = (
                                ""
                                if table == "user_sessions"
                                else ("JOIN user_sessions s ON s.id=x.session_id")
                            )
                            query = f"SELECT x.id FROM {table} x {join} WHERE {AUTH[table]}"
                        elif table in leaf:
                            query = f"SELECT id FROM {table} WHERE {leaf[table]}"
                        else:
                            predicate = (
                                "revoked_at<=:cutoff"
                                if table == "share_links"
                                else ("status='archived' AND archived_at<=:cutoff")
                            )
                            query = f"SELECT id FROM {table} WHERE {predicate}"
                        ids = (
                            (
                                await connection.execute(
                                    sa.text(
                                        "SELECT id FROM ("
                                        + query
                                        + ") candidates WHERE (CAST(:after AS uuid) IS NULL OR "
                                        "id>CAST(:after AS uuid)) ORDER BY id LIMIT :limit"
                                    ),
                                    {
                                        "at": at,
                                        "cutoff": cutoff,
                                        "limit": batch_size,
                                        "after": after_id,
                                    },
                                )
                            )
                            .scalars()
                            .all()
                        )
                    progress = 0
                    for identity_ in ids:
                        async with connection.begin():
                            await identity(connection, project, run_id, target.database)
                            await connection.execute(sa.text("SET LOCAL lock_timeout='2s'"))
                            if table in AUTH:
                                progress += await _auth_one(connection, table, identity_, at)
                            elif table in leaf:
                                current = await connection.scalar(
                                    sa.text(
                                        f"SELECT id FROM {table} "
                                        f"WHERE id=:id AND ({leaf[table]}) FOR UPDATE SKIP LOCKED"
                                    ),
                                    {"id": identity_, "at": at},
                                )
                                progress += (
                                    await _delete_ids(connection, table, [current])
                                    if current
                                    else 0
                                )
                            else:
                                progress += await _content_one(connection, table, identity_, cutoff)
                    removed[table] += progress
                    if len(ids) < batch_size:
                        break
                    after_id = ids[-1]
            async with connection.begin():
                after = await cleanup_plan(connection, at, with_audit=with_audit)
            return {
                "executed": True,
                "project": project,
                "run_id": run_id,
                "cutoff": at.isoformat(),
                "removed": removed,
                "count_semantics": "direct category resources; FK-dependent rows are removed "
                "with their parent groups",
                "remaining": after,
            }
    finally:
        await engine.dispose()
