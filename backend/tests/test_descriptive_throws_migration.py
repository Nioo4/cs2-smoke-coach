"""Real PostgreSQL coverage for the non-destructive descriptive-throws migration."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

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
    """Provision one disposable database on the explicitly isolated test PG."""

    if os.environ.get("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("real migration coverage requires an explicit isolated test stack")
    if os.environ.get("APP_ENV") != "test":
        pytest.fail("real migration coverage must run with APP_ENV=test", pytrace=False)
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.fail("real migration coverage requires DATABASE_URL", pytrace=False)
    try:
        parsed = make_url(raw_url)
    except (TypeError, ValueError):
        pytest.fail("real migration coverage received an invalid database URL", pytrace=False)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host != "postgres"
        or parsed.port != 5432
        or parsed.database != "smokeline"
        or parsed.username != "smokeline"
    ):
        pytest.fail(
            "real migration coverage requires the isolated postgres test database", pytrace=False
        )

    settings = app_config.get_settings()
    database_name = f"smokeline_mig_{uuid.uuid4().hex[:16]}"
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    try:
        with psycopg.connect(_sync_url(admin_url), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(database_name)))
    except psycopg.Error:
        pytest.fail("could not create the isolated migration database", pytrace=False)

    isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
    isolated_settings = settings.model_copy(update={"database_url": isolated_url})
    monkeypatch.setattr(app_config, "get_settings", lambda: isolated_settings)
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
            pytest.fail("could not remove the isolated migration database", pytrace=False)


def _insert_legacy_rows(database_url: str) -> dict[str, object]:
    target_id = uuid.uuid4()
    origin_id = uuid.uuid4()
    lineup_id = uuid.uuid4()
    video_id = uuid.uuid4()
    storage_key = uuid.uuid4()
    conversation_id = uuid.uuid4()
    now = datetime.now(UTC)
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM throw_methods WHERE slug = 'jump-throw'")
        method_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO targets
                (id, slug, name_zh, name_en, description, map_x, map_y, sort_order,
                 is_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)
            """,
            (
                target_id,
                "migration-vip",
                "迁移 VIP",
                "Migration VIP",
                "legacy target",
                50,
                25,
                3,
                now,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO origins
                (id, slug, name_zh, name_en, description, map_x, map_y, sort_order,
                 is_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)
            """,
            (
                origin_id,
                "migration-t-spawn",
                "迁移 T 出生点",
                "Migration T Spawn",
                "legacy origin",
                20,
                80,
                4,
                now,
                now,
            ),
        )
        cursor.execute(
            "INSERT INTO target_aliases (id, target_id, alias, normalized_alias) "
            "VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), target_id, "迁移别名", "迁移别名"),
        )
        cursor.execute(
            """
            INSERT INTO lineups
                (id, target_id, origin_id, throw_method_id, setup_text, aim_text, throw_text,
                 result_text, is_default, default_needs_review, sort_order, status,
                 last_checked_at, game_version_note, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, false, 1, 'published',
                    %s, %s, %s, %s)
            """,
            (
                lineup_id,
                target_id,
                origin_id,
                method_id,
                "legacy setup",
                "legacy aim",
                "legacy throw text",
                "legacy result",
                now,
                "legacy version",
                now,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO tutorial_videos
                (id, lineup_id, status, storage_key, relative_path, poster_relative_path,
                 duration_ms, size_bytes, checksum_sha256, rights_note, failure_reason,
                 created_at, updated_at)
            VALUES (%s, %s, 'ready', %s, %s, %s, 1500, 100, %s, %s, NULL, %s, %s)
            """,
            (
                video_id,
                lineup_id,
                storage_key,
                f"{lineup_id}/legacy.mp4",
                f"{lineup_id}/legacy.webp",
                "a" * 64,
                "legacy authorized fixture",
                now,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO conversation_states
                (id, map, grenade, target_slug, origin_slug, throw_method_slug,
                 pending_field, pending_options, expires_at, updated_at)
            VALUES (%s, 'mirage', 'smoke', %s, %s, 'jump-throw', NULL, NULL, %s, %s)
            """,
            (
                conversation_id,
                "migration-vip",
                "migration-t-spawn",
                now + timedelta(minutes=30),
                now,
            ),
        )
    return {
        "target_id": target_id,
        "origin_id": origin_id,
        "lineup_id": lineup_id,
        "video_id": video_id,
        "storage_key": storage_key,
        "conversation_id": conversation_id,
        "method_id": method_id,
    }


def _database_snapshot(database_url: str) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture every row so a blocked downgrade can be proven lossless."""

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
    with _connect(database_url) as connection, connection.cursor() as cursor:
        snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table, order_column in tables:
            cursor.execute(f"SELECT * FROM {table} ORDER BY {order_column}::text")
            snapshot[table] = tuple(tuple(row) for row in cursor.fetchall())
        return snapshot


def _migration_version(database_url: str) -> str | None:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
    return row[0] if row else None


@pytest.mark.integration
def test_descriptive_throws_migration_is_lossless_and_guards_downgrade(
    migration_database: str,
) -> None:
    database_url = migration_database
    # This file verifies the historical 0002 contract.  Keep its target
    # explicit now that 0003 is the repository head.
    _run_migration(database_url, "0002_descriptive_throws")
    assert _migration_version(database_url) == "0002_descriptive_throws"
    _run_migration(database_url, "base", downgrade=True)
    assert _migration_version(database_url) is None
    _run_migration(database_url, "0002_descriptive_throws")
    _run_migration(database_url, "0001_initial", downgrade=True)
    ids = _insert_legacy_rows(database_url)
    before_upgrade = _database_snapshot(database_url)

    _run_migration(database_url, "0002_descriptive_throws")
    assert _migration_version(database_url) == "0002_descriptive_throws"
    assert _database_snapshot(database_url) == before_upgrade

    for status in ("draft", "published", "archived"):
        null_lineup_id = uuid.uuid4()
        with _connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO lineups
                    (id, target_id, origin_id, setup_text, aim_text, throw_text, result_text,
                     status, is_default, default_needs_review)
                VALUES (%s, %s, %s, 'new setup', 'new aim', 'new throw', 'new result',
                        %s, false, false)
                """,
                (null_lineup_id, ids["target_id"], ids["origin_id"], status),
            )
        before_blocked_null = _database_snapshot(database_url)
        with pytest.raises(RuntimeError, match="DESCRIPTIVE_THROW_DOWNGRADE_BLOCKED"):
            _run_migration(database_url, "0001_initial", downgrade=True)
        assert _migration_version(database_url) == "0002_descriptive_throws"
        assert _database_snapshot(database_url) == before_blocked_null
        with _connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM lineups WHERE id = %s", (null_lineup_id,))

    for status in ("draft", "published", "archived"):
        duplicate_ids = [uuid.uuid4(), uuid.uuid4()]
        with _connect(database_url) as connection, connection.cursor() as cursor:
            for duplicate_id in duplicate_ids:
                cursor.execute(
                    """
                    INSERT INTO lineups
                        (id, target_id, origin_id, throw_method_id, setup_text, aim_text,
                         throw_text, result_text, status, is_default, default_needs_review)
                    VALUES (%s, %s, %s, %s, 'variant setup', 'variant aim', 'variant throw',
                            'variant result', %s, false, false)
                    """,
                    (
                        duplicate_id,
                        ids["target_id"],
                        ids["origin_id"],
                        ids["method_id"],
                        status,
                    ),
                )
        before_blocked_duplicate = _database_snapshot(database_url)
        with pytest.raises(RuntimeError, match="DESCRIPTIVE_THROW_DOWNGRADE_BLOCKED"):
            _run_migration(database_url, "0001_initial", downgrade=True)
        assert _migration_version(database_url) == "0002_descriptive_throws"
        assert _database_snapshot(database_url) == before_blocked_duplicate
        with _connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM lineups WHERE id = ANY(%s)", (duplicate_ids,))

    _run_migration(database_url, "0001_initial", downgrade=True)
    assert _migration_version(database_url) == "0001_initial"
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'lineups' "
            "AND column_name = 'throw_method_id'"
        )
        assert cursor.fetchone()[0] == "NO"
        cursor.execute("SELECT 1 FROM pg_constraint WHERE conname = 'uq_lineup_identity'")
        assert cursor.fetchone() == (1,)
    assert _database_snapshot(database_url) == before_upgrade
    _run_migration(database_url, "0002_descriptive_throws")
    assert _migration_version(database_url) == "0002_descriptive_throws"
