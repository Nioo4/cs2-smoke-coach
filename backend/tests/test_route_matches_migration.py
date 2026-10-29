"""Real PostgreSQL coverage for the Phase 5B response-type migration."""

from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from sqlalchemy.exc import OperationalError
from test_hd_media_migration import _connect, _run_migration, _sync_url, _version

pytest_plugins = ("test_hd_media_migration",)


_BASELINE_MIGRATION_HASHES = {
    "0001_initial_schema.py": ("7AB9B4DD4EAA4CF31C28D129F91235930579AC40977867BA9454806BE4E2B4D6"),
    "0002_descriptive_throws.py": (
        "7CCED22E28FF084BE03D933DDF9E98D43C6C90AB5E08F2BD554435627A65DCF6"
    ),
    "0003_optional_coordinates.py": (
        "C9E946E392D09C76F03E16D08348A7EA4021AF6E5A37A186CDD82E388FF9052A"
    ),
    "0004_hd_media_output.py": ("AFBF465CBC9B979F7B1838A6A044430D6D358A2DA4E39469D3B3F6D2CE892E81"),
}


def _response_check(database_url: str) -> str:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid, true) FROM pg_constraint "
            "WHERE conrelid = 'usage_events'::regclass AND conname = 'ck_usage_response_type'"
        )
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def _usage_rows(database_url: str) -> tuple[tuple[object, ...], ...]:
    """Snapshot every usage column, including all fields unrelated to the CHECK."""

    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, request_id, event_type, input_mode, response_type, target_slug, "
            "origin_slug, latency_ms, provider_type, reserved_units, provider_outcome, "
            "input_tokens, output_tokens, error_code, created_at::text "
            "FROM usage_events ORDER BY id::text"
        )
        return tuple(tuple(row) for row in cursor.fetchall())


def _insert_event(
    database_url: str,
    response_type: str | None,
    *,
    target_slug: str = "vip",
    origin_slug: str = "t-spawn",
    input_mode: str = "text",
    error_code: str | None = "fixture_error",
) -> None:
    with _connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO usage_events "
            "(id, request_id, event_type, input_mode, response_type, target_slug, origin_slug, "
            "latency_ms, error_code) "
            "VALUES (%s, %s, 'public_request', %s, %s, %s, %s, 17, %s)",
            (
                uuid.uuid4(),
                uuid.uuid4(),
                input_mode,
                response_type,
                target_slug,
                origin_slug,
                error_code,
            ),
        )


def test_phase5b_migration_history_hashes_are_fixed() -> None:
    versions = Path(__file__).parents[1] / "alembic" / "versions"
    for filename, expected in _BASELINE_MIGRATION_HASHES.items():
        actual = hashlib.sha256((versions / filename).read_bytes()).hexdigest().upper()
        assert actual == expected, filename


@pytest.mark.integration
def test_empty_route_matches_migration_round_trip(migration_database: str) -> None:
    database_url = migration_database
    _run_migration(database_url, "0004_hd_media_output")
    assert _version(database_url) == "0004_hd_media_output"
    assert _usage_rows(database_url) == ()

    _run_migration(database_url, "0005_route_matches_response")
    assert _version(database_url) == "0005_route_matches_response"
    assert "route_matches" in _response_check(database_url)

    _run_migration(database_url, "0004_hd_media_output", downgrade=True)
    assert _version(database_url) == "0004_hd_media_output"
    assert "route_matches" not in _response_check(database_url)

    _run_migration(database_url, "0005_route_matches_response")
    assert _version(database_url) == "0005_route_matches_response"
    assert "route_matches" in _response_check(database_url)


@pytest.mark.integration
def test_route_matches_migration_preserves_all_history_and_guards_downgrade(
    migration_database: str,
) -> None:
    database_url = migration_database
    _run_migration(database_url, "0004_hd_media_output")
    for response_type in (
        "match",
        "target_matches",
        "clarification",
        "external_candidates",
        "not_found",
        "out_of_scope",
        None,
    ):
        _insert_event(database_url, response_type)
    before_upgrade = _usage_rows(database_url)
    before_constraint = _response_check(database_url)

    _run_migration(database_url, "0005_route_matches_response")
    assert _version(database_url) == "0005_route_matches_response"
    assert "route_matches" in _response_check(database_url)
    assert _usage_rows(database_url) == before_upgrade

    _insert_event(database_url, "route_matches")
    blocked_snapshot = _usage_rows(database_url)
    blocked_constraint = _response_check(database_url)
    blocked_version = _version(database_url)
    with pytest.raises(RuntimeError, match="ROUTE_MATCHES_RESPONSE_DOWNGRADE_BLOCKED"):
        _run_migration(database_url, "0004_hd_media_output", downgrade=True)
    assert _version(database_url) == blocked_version
    assert _response_check(database_url) == blocked_constraint
    assert _usage_rows(database_url) == blocked_snapshot

    with _connect(database_url) as connection:
        connection.execute("DELETE FROM usage_events WHERE response_type = 'route_matches'")
    _run_migration(database_url, "0004_hd_media_output", downgrade=True)
    assert _version(database_url) == "0004_hd_media_output"
    assert _response_check(database_url) == before_constraint
    assert _usage_rows(database_url) == before_upgrade

    _run_migration(database_url, "0005_route_matches_response")
    assert _version(database_url) == "0005_route_matches_response"
    assert _usage_rows(database_url) == before_upgrade


def _assert_locked_migration(
    database_url: str,
    revision: str,
    *,
    downgrade: bool,
    expected_version: str,
    expected_check_fragment: str,
) -> None:
    blocker = psycopg.connect(_sync_url(database_url), autocommit=False)
    result: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        blocker.execute('LOCK TABLE "usage_events" IN ACCESS EXCLUSIVE MODE')

        def migrate() -> None:
            try:
                _run_migration(database_url, revision, downgrade=downgrade)
            except BaseException as exc:  # pragma: no cover - integration failure path
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
    assert _version(database_url) == expected_version
    assert expected_check_fragment in _response_check(database_url)


@pytest.mark.integration
def test_route_matches_migration_honors_bounded_table_lock_both_directions(
    migration_database: str,
) -> None:
    database_url = migration_database
    _run_migration(database_url, "0004_hd_media_output")
    _assert_locked_migration(
        database_url,
        "0005_route_matches_response",
        downgrade=False,
        expected_version="0004_hd_media_output",
        expected_check_fragment="target_matches",
    )

    _run_migration(database_url, "0005_route_matches_response")
    _assert_locked_migration(
        database_url,
        "0004_hd_media_output",
        downgrade=True,
        expected_version="0005_route_matches_response",
        expected_check_fragment="route_matches",
    )
