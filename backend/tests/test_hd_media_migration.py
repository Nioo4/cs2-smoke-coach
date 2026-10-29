"""Real PostgreSQL regression coverage for the v1.4 media-size migration."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors, sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from alembic import command
from app.core import config as app_config

_LEGACY_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = 12 * 1024 * 1024


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
        pytest.skip("real HD media migration coverage requires an explicit isolated test stack")
    if os.environ.get("APP_ENV") != "test":
        pytest.fail("real HD media migration coverage must run with APP_ENV=test", pytrace=False)
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.fail("real HD media migration coverage requires DATABASE_URL", pytrace=False)
    try:
        parsed = make_url(raw_url)
    except (TypeError, ValueError):
        pytest.fail("real HD media migration received an invalid database URL", pytrace=False)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host != "postgres"
        or parsed.port != 5432
        or parsed.database != "smokeline"
        or parsed.username != "smokeline"
    ):
        pytest.fail(
            "real HD media migration requires the isolated PostgreSQL test database",
            pytrace=False,
        )

    settings = app_config.get_settings()
    database_name = f"smokeline_hd_{uuid.uuid4().hex[:16]}"
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    try:
        with psycopg.connect(_sync_url(admin_url), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(database_name)))
    except psycopg.Error:
        pytest.fail("could not create the isolated HD migration database", pytrace=False)

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
            pytest.fail("could not remove the isolated HD migration database", pytrace=False)


def _version(database_url: str) -> str | None:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
    return row[0] if row else None


def _video_rows(database_url: str) -> tuple[tuple[object, ...], ...]:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM tutorial_videos ORDER BY id::text")
        return tuple(tuple(row) for row in cursor.fetchall())


def _video_size_check(database_url: str) -> str:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid, true) FROM pg_constraint "
            "WHERE conrelid = 'tutorial_videos'::regclass AND conname = 'ck_video_size'"
        )
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def _insert_catalog_and_media_rows(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    target_id, origin_id = uuid.uuid4(), uuid.uuid4()
    now_sql = "CURRENT_TIMESTAMP"
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO targets "
            "(id, slug, name_zh, description, map_x, map_y, sort_order, is_active, "
            "created_at, updated_at) "
            f"VALUES (%s, %s, %s, '', NULL, NULL, 1, true, {now_sql}, {now_sql})",
            (target_id, f"hd-target-{target_id.hex[:8]}", "高清目标"),
        )
        cursor.execute(
            "INSERT INTO origins "
            "(id, slug, name_zh, description, map_x, map_y, sort_order, is_active, "
            "created_at, updated_at) "
            f"VALUES (%s, %s, %s, '', NULL, NULL, 1, true, {now_sql}, {now_sql})",
            (origin_id, f"hd-origin-{origin_id.hex[:8]}", "高清起点"),
        )
        for index, (lineup_status, video_status, size_bytes) in enumerate(
            (
                ("draft", "ready", _LEGACY_MAX_OUTPUT_BYTES),
                ("archived", "archived", _LEGACY_MAX_OUTPUT_BYTES),
                ("draft", "failed", None),
            )
        ):
            lineup_id, video_id, storage_key = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            cursor.execute(
                "INSERT INTO lineups "
                "(id, target_id, origin_id, setup_text, aim_text, throw_text, result_text, "
                "is_default, default_needs_review, sort_order, status, created_at, updated_at) "
                f"VALUES (%s, %s, %s, 'setup', 'aim', 'throw', 'result', false, false, "
                f"%s, %s, {now_sql}, {now_sql})",
                (lineup_id, target_id, origin_id, index, lineup_status),
            )
            if video_status == "failed":
                cursor.execute(
                    "INSERT INTO tutorial_videos "
                    "(id, lineup_id, status, rights_note, failure_reason, created_at, updated_at) "
                    f"VALUES (%s, %s, 'failed', 'migration fixture', 'processing_failed', "
                    f"{now_sql}, {now_sql})",
                    (video_id, lineup_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO tutorial_videos "
                    "(id, lineup_id, status, storage_key, relative_path, poster_relative_path, "
                    "duration_ms, size_bytes, checksum_sha256, rights_note, created_at, "
                    "updated_at) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, 1000, %s, %s, 'migration fixture', "
                    f"{now_sql}, {now_sql})",
                    (
                        video_id,
                        lineup_id,
                        video_status,
                        storage_key,
                        f"{lineup_id}/primary-{storage_key}.mp4",
                        f"{lineup_id}/primary-{storage_key}.webp",
                        size_bytes,
                        "a" * 64,
                    ),
                )
    return target_id, origin_id


@pytest.mark.integration
def test_hd_media_migration_is_lossless_and_guards_downgrade(migration_database: str) -> None:
    database_url = migration_database
    _run_migration(database_url, "0003_optional_coordinates")
    _insert_catalog_and_media_rows(database_url)
    before_upgrade = _video_rows(database_url)

    _run_migration(database_url, "0004_hd_media_output")
    assert _version(database_url) == "0004_hd_media_output"
    assert str(_MAX_OUTPUT_BYTES) in _video_size_check(database_url)
    assert _video_rows(database_url) == before_upgrade

    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM tutorial_videos WHERE status = 'ready'")
        ready_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE tutorial_videos SET size_bytes = %s WHERE id = %s",
            (_MAX_OUTPUT_BYTES, ready_id),
        )
        cursor.execute("SELECT size_bytes FROM tutorial_videos WHERE id = %s", (ready_id,))
        assert cursor.fetchone()[0] == _MAX_OUTPUT_BYTES
        with pytest.raises(errors.CheckViolation):
            cursor.execute(
                "UPDATE tutorial_videos SET size_bytes = %s WHERE id = %s",
                (_MAX_OUTPUT_BYTES + 1, ready_id),
            )
        cursor.execute("SELECT size_bytes FROM tutorial_videos WHERE id = %s", (ready_id,))
        assert cursor.fetchone()[0] == _MAX_OUTPUT_BYTES

    blocked_snapshot = _video_rows(database_url)
    blocked_constraint = _video_size_check(database_url)
    with pytest.raises(RuntimeError, match="HD_MEDIA_DOWNGRADE_BLOCKED"):
        _run_migration(database_url, "0003_optional_coordinates", downgrade=True)
    assert _version(database_url) == "0004_hd_media_output"
    assert _video_rows(database_url) == blocked_snapshot
    assert _video_size_check(database_url) == blocked_constraint

    with _connect(database_url) as connection:
        connection.execute(
            "UPDATE tutorial_videos SET size_bytes = %s WHERE size_bytes = %s",
            (_LEGACY_MAX_OUTPUT_BYTES, _MAX_OUTPUT_BYTES),
        )
    _run_migration(database_url, "0003_optional_coordinates", downgrade=True)
    assert _version(database_url) == "0003_optional_coordinates"
    assert str(_LEGACY_MAX_OUTPUT_BYTES) in _video_size_check(database_url)
    _run_migration(database_url, "0004_hd_media_output")
    assert _version(database_url) == "0004_hd_media_output"
    assert str(_MAX_OUTPUT_BYTES) in _video_size_check(database_url)


@pytest.mark.integration
def test_hd_media_migration_honors_bounded_table_lock(migration_database: str) -> None:
    database_url = migration_database
    _run_migration(database_url, "0003_optional_coordinates")

    def run_locked_migration(
        revision: str, *, downgrade: bool
    ) -> tuple[str | None, str, BaseException]:
        before_version = _version(database_url)
        before_constraint = _video_size_check(database_url)
        blocker = psycopg.connect(_sync_url(database_url), autocommit=False)
        result: list[BaseException] = []
        worker: threading.Thread | None = None
        try:
            blocker.execute('LOCK TABLE "tutorial_videos" IN ACCESS EXCLUSIVE MODE')

            def migrate() -> None:
                try:
                    _run_migration(database_url, revision, downgrade=downgrade)
                except BaseException as exc:
                    result.append(exc)

            worker = threading.Thread(target=migrate)
            worker.start()
            worker.join(timeout=9)
        finally:
            blocker.rollback()
            blocker.close()
            if worker is not None:
                worker.join(timeout=3)
        assert worker is not None and not worker.is_alive()
        assert result and isinstance(result[0], OperationalError)
        assert isinstance(result[0].orig, errors.LockNotAvailable)
        assert _version(database_url) == before_version
        assert _video_size_check(database_url) == before_constraint
        return before_version, before_constraint, result[0]

    before_version, before_constraint, _ = run_locked_migration(
        "0004_hd_media_output", downgrade=False
    )
    assert before_version == "0003_optional_coordinates"
    assert str(_LEGACY_MAX_OUTPUT_BYTES) in before_constraint

    _run_migration(database_url, "0004_hd_media_output")
    before_version, before_constraint, _ = run_locked_migration(
        "0003_optional_coordinates", downgrade=True
    )
    assert before_version == "0004_hd_media_output"
    assert str(_MAX_OUTPUT_BYTES) in before_constraint


def test_hd_media_migration_keeps_frozen_old_migration_hashes() -> None:
    expected = {
        "0001_initial_schema.py": (
            "7AB9B4DD4EAA4CF31C28D129F91235930579AC40977867BA9454806BE4E2B4D6"
        ),
        "0002_descriptive_throws.py": (
            "7CCED22E28FF084BE03D933DDF9E98D43C6C90AB5E08F2BD554435627A65DCF6"
        ),
        "0003_optional_coordinates.py": (
            "C9E946E392D09C76F03E16D08348A7EA4021AF6E5A37A186CDD82E388FF9052A"
        ),
    }
    versions_dir = Path(__file__).parents[1] / "alembic" / "versions"
    for filename, digest in expected.items():
        assert hashlib.sha256((versions_dir / filename).read_bytes()).hexdigest().upper() == digest
