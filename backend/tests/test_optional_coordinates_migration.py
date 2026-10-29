"""Real PostgreSQL regression coverage for the v1.3 coordinate migration."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors, sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from alembic import command
from app.core import config as app_config


@pytest.fixture(autouse=True)
def restore_application_logging() -> Iterator[None]:
    """Keep Alembic's fileConfig from leaking handlers into later tests."""

    yield
    from app.core.logging import configure_logging

    configure_logging()


def _sync_url(raw_url: str) -> str:
    return raw_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _migration_config(database_url: str) -> Config:
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _run_migration(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    config = _migration_config(database_url)
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


def _connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(_sync_url(database_url), autocommit=True)


@pytest.fixture()
def migration_database(monkeypatch) -> Iterator[str]:
    if os.environ.get("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("real coordinate migration coverage requires an explicit isolated test stack")
    if os.environ.get("APP_ENV") != "test":
        pytest.fail("real coordinate migration coverage must run with APP_ENV=test", pytrace=False)
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.fail("real coordinate migration coverage requires DATABASE_URL", pytrace=False)
    try:
        parsed = make_url(raw_url)
    except (TypeError, ValueError):
        pytest.fail(
            "real coordinate migration coverage received an invalid database URL",
            pytrace=False,
        )
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host != "postgres"
        or parsed.port != 5432
        or parsed.database != "smokeline"
        or parsed.username != "smokeline"
    ):
        pytest.fail(
            "real coordinate migration requires the isolated PostgreSQL test database",
            pytrace=False,
        )

    settings = app_config.get_settings()
    database_name = f"smokeline_coord_{uuid.uuid4().hex[:16]}"
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    try:
        with psycopg.connect(_sync_url(admin_url), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(database_name)))
    except psycopg.Error:
        pytest.fail("could not create the isolated coordinate migration database", pytrace=False)

    isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setattr(
        app_config,
        "get_settings",
        lambda: settings.model_copy(update={"database_url": isolated_url}),
    )
    try:
        yield isolated_url
    finally:
        try:
            with psycopg.connect(_sync_url(admin_url), autocommit=True) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                admin.execute(sql.SQL("DROP DATABASE {} ").format(sql.Identifier(database_name)))
        except psycopg.Error:
            pytest.fail(
                "could not remove the isolated coordinate migration database", pytrace=False
            )


def _version(database_url: str) -> str | None:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
    return row[0] if row else None


def _database_snapshot(database_url: str) -> dict[str, tuple[tuple[object, ...], ...]]:
    snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
    with _connect(database_url) as connection, connection.cursor() as cursor:
        tables = (
            ("targets", "id"),
            ("target_aliases", "id"),
            ("origins", "id"),
            ("origin_aliases", "id"),
            ("throw_methods", "id"),
            ("throw_method_aliases", "id"),
            ("lineups", "id"),
            ("tutorial_videos", "id"),
            ("conversation_states", "id"),
            ("external_search_cache", "cache_key"),
            ("usage_events", "id"),
        )
        for table, order_column in tables:
            cursor.execute(f"SELECT * FROM {table} ORDER BY {order_column}::text")
            snapshot[table] = tuple(tuple(row) for row in cursor.fetchall())
    return snapshot


def _point_schema_snapshot(database_url: str) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture the point-table schema used by the no-loss downgrade guard."""

    snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, a.attname, a.attnotnull,
                   format_type(a.atttypid, a.atttypmod),
                   pg_get_expr(d.adbin, d.adrelid)
            FROM pg_class AS c
            JOIN pg_attribute AS a ON a.attrelid = c.oid
            LEFT JOIN pg_attrdef AS d
              ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE c.relnamespace = 'public'::regnamespace
              AND c.relname IN ('targets', 'origins')
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
            """
        )
        snapshot["columns"] = tuple(tuple(row) for row in cursor.fetchall())
        cursor.execute(
            """
            SELECT conrelid::regclass::text, conname, contype,
                   pg_get_constraintdef(oid, true)
            FROM pg_constraint
            WHERE conrelid IN ('targets'::regclass, 'origins'::regclass)
            ORDER BY conrelid::regclass::text, conname
            """
        )
        snapshot["constraints"] = tuple(tuple(row) for row in cursor.fetchall())
        cursor.execute(
            """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename IN ('targets', 'origins')
            ORDER BY tablename, indexname
            """
        )
        snapshot["indexes"] = tuple(tuple(row) for row in cursor.fetchall())
    return snapshot


def _insert_point(
    database_url: str,
    table: str,
    point_id: uuid.UUID,
    slug: str,
    map_x: object,
    map_y: object,
    *,
    active: bool = True,
) -> None:
    with _connect(database_url) as connection:
        connection.execute(
            sql.SQL(
                "INSERT INTO {} "
                "(id, slug, name_zh, name_en, description, map_x, map_y, sort_order, "
                "is_active, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ).format(sql.Identifier(table)),
            (point_id, slug, slug, None, "坐标迁移测试", map_x, map_y, active),
        )


def _insert_related_rows(database_url: str, target_id: uuid.UUID, origin_id: uuid.UUID) -> None:
    lineup_id = uuid.uuid4()
    video_id = uuid.uuid4()
    storage_key = uuid.uuid4()
    conversation_id = uuid.uuid4()
    now = datetime.now(UTC)
    with _connect(database_url) as connection:
        method_id = connection.execute(
            "SELECT id FROM throw_methods WHERE slug = 'jump-throw'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO target_aliases (id, target_id, alias, normalized_alias) "
            "VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), target_id, "坐标迁移目标别名", "坐标迁移目标别名"),
        )
        connection.execute(
            "INSERT INTO origin_aliases (id, origin_id, alias, normalized_alias) "
            "VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), origin_id, "坐标迁移起点别名", "坐标迁移起点别名"),
        )
        connection.execute(
            """
            INSERT INTO lineups
                (id, target_id, origin_id, throw_method_id, setup_text, aim_text, throw_text,
                 result_text, is_default, default_needs_review, sort_order, status,
                 last_checked_at, game_version_note, created_at, updated_at)
            VALUES (%s, %s, %s, %s, '迁移站位', '迁移瞄点', '迁移普通文字', '迁移结果',
                    true, false, 1, 'published', %s, '迁移版本', %s, %s)
            """,
            (lineup_id, target_id, origin_id, method_id, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO tutorial_videos
                (id, lineup_id, status, storage_key, relative_path, poster_relative_path,
                 duration_ms, size_bytes, checksum_sha256, rights_note, failure_reason,
                 created_at, updated_at)
            VALUES (%s, %s, 'ready', %s, %s, %s, 1500, 100, %s, '迁移测试授权', NULL, %s, %s)
            """,
            (
                video_id,
                lineup_id,
                storage_key,
                f"{lineup_id}/coord.mp4",
                f"{lineup_id}/coord.webp",
                "c" * 64,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_states
                (id, map, grenade, target_slug, origin_slug, throw_method_slug,
                 pending_field, pending_options, expires_at, updated_at)
            VALUES (%s, 'mirage', 'smoke', %s, %s, 'jump-throw', NULL, NULL, %s, %s)
            """,
            (conversation_id, "coord-target", "coord-origin", now + timedelta(minutes=30), now),
        )
        connection.execute(
            """
            INSERT INTO external_search_cache
                (cache_key, payload, is_empty, expires_at, created_at)
            VALUES ('v3|migration-coordinate', '[]'::jsonb, true, %s, %s)
            """,
            (now + timedelta(hours=1), now),
        )
        connection.execute(
            """
            INSERT INTO usage_events
                (id, request_id, event_type, input_mode, response_type, target_slug, origin_slug,
                 latency_ms, provider_type, reserved_units, provider_outcome, input_tokens,
                 output_tokens, error_code, created_at)
            VALUES (%s, %s, 'public_request', 'text', 'match', 'coord-target', 'coord-origin',
                    7, NULL, 0, NULL, 0, 0, NULL, %s)
            """,
            (uuid.uuid4(), uuid.uuid4(), now),
        )


@pytest.mark.integration
def test_optional_coordinate_migration_is_lossless_and_pair_safe(
    migration_database: str,
) -> None:
    database_url = migration_database
    _run_migration(database_url, "0002_descriptive_throws")
    schema_before_upgrade = _point_schema_snapshot(database_url)
    target_id = uuid.uuid4()
    origin_id = uuid.uuid4()
    _insert_point(
        database_url,
        "targets",
        target_id,
        f"coord-target-{target_id.hex[:8]}",
        12.34,
        56.78,
    )
    _insert_point(
        database_url,
        "origins",
        origin_id,
        f"coord-origin-{origin_id.hex[:8]}",
        87.65,
        43.21,
        active=False,
    )
    _insert_related_rows(database_url, target_id, origin_id)
    before_upgrade = _database_snapshot(database_url)

    _run_migration(database_url, "0003_optional_coordinates")
    schema_after_upgrade = _point_schema_snapshot(database_url)
    assert schema_after_upgrade["columns"] == tuple(
        (table, name, False if name in ("map_x", "map_y") else not_null, column_type, default)
        for table, name, not_null, column_type, default in schema_before_upgrade["columns"]
    )
    assert schema_after_upgrade["indexes"] == schema_before_upgrade["indexes"]
    assert (
        tuple(
            constraint
            for constraint in schema_after_upgrade["constraints"]
            if constraint[1] not in ("ck_targets_coordinate_pair", "ck_origins_coordinate_pair")
        )
        == schema_before_upgrade["constraints"]
    )
    assert _version(database_url) == "0003_optional_coordinates"
    with _connect(database_url) as connection, connection.cursor() as cursor:
        for table in ("targets", "origins"):
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "AND column_name IN ('map_x', 'map_y') ORDER BY column_name",
                (table,),
            )
            assert [row[0] for row in cursor.fetchall()] == ["YES", "YES"]
            cursor.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
                (table, f"ck_{table}_coordinate_pair"),
            )
            assert cursor.fetchone() == (f"ck_{table}_coordinate_pair",)
    assert _database_snapshot(database_url) == before_upgrade

    for table, map_x, map_y in (
        ("targets", None, 1),
        ("targets", 1, None),
        ("origins", None, 1),
        ("origins", 1, None),
    ):
        invalid_id = uuid.uuid4()
        with pytest.raises(errors.CheckViolation):
            _insert_point(
                database_url,
                table,
                invalid_id,
                f"coord-half-{invalid_id.hex[:8]}",
                map_x,
                map_y,
            )

    for table, value in (("targets", -1), ("targets", 101), ("origins", -1), ("origins", 101)):
        invalid_id = uuid.uuid4()
        with pytest.raises(errors.CheckViolation):
            _insert_point(
                database_url,
                table,
                invalid_id,
                f"coord-out-of-range-{invalid_id.hex[:8]}",
                value,
                value,
            )

    for table in ("targets", "origins"):
        point_id = uuid.uuid4()
        _insert_point(
            database_url,
            table,
            point_id,
            f"coord-null-{table}-{point_id.hex[:8]}",
            None,
            None,
            active=False,
        )
        blocked_snapshot = _database_snapshot(database_url)
        blocked_schema = _point_schema_snapshot(database_url)
        with pytest.raises(RuntimeError, match="OPTIONAL_COORDINATES_DOWNGRADE_BLOCKED"):
            _run_migration(database_url, "0002_descriptive_throws", downgrade=True)
        assert _version(database_url) == "0003_optional_coordinates"
        assert _database_snapshot(database_url) == blocked_snapshot
        assert _point_schema_snapshot(database_url) == blocked_schema
        with _connect(database_url) as connection:
            connection.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table)),
                (point_id,),
            )

    for table in ("targets", "origins"):
        boundary_id = uuid.uuid4()
        _insert_point(
            database_url,
            table,
            boundary_id,
            f"coord-boundary-{table}-{boundary_id.hex[:8]}",
            0,
            100,
        )
    before_downgrade = _database_snapshot(database_url)
    _run_migration(database_url, "0002_descriptive_throws", downgrade=True)
    assert _version(database_url) == "0002_descriptive_throws"
    with _connect(database_url) as connection, connection.cursor() as cursor:
        for table in ("targets", "origins"):
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "AND column_name IN ('map_x', 'map_y') ORDER BY column_name",
                (table,),
            )
            assert [row[0] for row in cursor.fetchall()] == ["NO", "NO"]
            cursor.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = %s",
                (f"ck_{table}_coordinate_pair",),
            )
            assert cursor.fetchone() is None
    assert _database_snapshot(database_url) == before_downgrade
    assert _point_schema_snapshot(database_url) == schema_before_upgrade
    _run_migration(database_url, "0003_optional_coordinates")
    assert _version(database_url) == "0003_optional_coordinates"
    assert _database_snapshot(database_url) == before_downgrade
    assert _point_schema_snapshot(database_url) == schema_after_upgrade


@pytest.mark.integration
def test_optional_coordinate_upgrade_honors_bounded_table_lock(
    migration_database: str,
) -> None:
    database_url = migration_database

    def run_locked(table: str, revision: str, *, downgrade: bool) -> None:
        before_version = _version(database_url)
        before_data = _database_snapshot(database_url)
        before_schema = _point_schema_snapshot(database_url)
        blocker = psycopg.connect(_sync_url(database_url), autocommit=False)
        result: list[BaseException] = []
        worker: threading.Thread | None = None

        try:
            blocker.execute(
                sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table))
            )

            def migrate() -> None:
                try:
                    _run_migration(database_url, revision, downgrade=downgrade)
                except BaseException as exc:  # capture the bounded-lock failure from the worker
                    result.append(exc)

            worker = threading.Thread(target=migrate)
            worker.start()
            worker.join(timeout=9)
        finally:
            blocker.rollback()
            blocker.close()
            if worker is not None:
                worker.join(timeout=3)
        assert worker is not None
        assert not worker.is_alive(), "coordinate migration left a lock worker running"
        assert result and isinstance(result[0], OperationalError)
        assert isinstance(result[0].orig, errors.LockNotAvailable)
        assert _version(database_url) == before_version
        assert _database_snapshot(database_url) == before_data
        assert _point_schema_snapshot(database_url) == before_schema

    _run_migration(database_url, "0002_descriptive_throws")
    for table in ("targets", "origins"):
        run_locked(table, "0003_optional_coordinates", downgrade=False)
    assert _version(database_url) == "0002_descriptive_throws"
    _run_migration(database_url, "0003_optional_coordinates")
    for table in ("targets", "origins"):
        run_locked(table, "0002_descriptive_throws", downgrade=True)
    assert _version(database_url) == "0003_optional_coordinates"
