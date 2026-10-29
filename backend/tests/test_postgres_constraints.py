"""PostgreSQL-only regression coverage for the frozen and retired schema."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import sqlalchemy as sa
from psycopg.types.json import Jsonb
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import ConversationState


@pytest.fixture(scope="module")
def database_url() -> str:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.fail("DATABASE_URL is required for PostgreSQL integration tests", pytrace=False)
    sync_url = raw_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(sync_url, connect_timeout=2):
            pass
    except psycopg.OperationalError:
        pytest.fail("PostgreSQL integration database is unavailable", pytrace=False)
    return raw_url


def _assert_insert(
    connection: psycopg.Connection,
    statement: str,
    parameters: tuple[object, ...],
    expected: bool,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT constraint_case")
        try:
            cursor.execute(statement, parameters)
        except psycopg.Error:
            accepted = False
        else:
            accepted = True
        finally:
            cursor.execute("ROLLBACK TO SAVEPOINT constraint_case")
            cursor.execute("RELEASE SAVEPOINT constraint_case")
    assert accepted is expected


def test_pending_state_check_rejects_null_and_non_string_boundaries(database_url: str) -> None:
    sync_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    statement = """
        INSERT INTO conversation_states
            (id, pending_field, pending_options, expires_at)
        VALUES (%s, %s, %s, %s)
    """
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    valid_cases = [
        (None, None),
        ("target", Jsonb(["vip"])),
        ("target", Jsonb(["vip", "short"])),
        ("target", Jsonb(["one", "two", "three", "four", "five", "six"])),
    ]
    invalid_cases = [
        ("target", None),
        (None, Jsonb(["vip"])),
        ("other", Jsonb(["vip"])),
        ("target", Jsonb([])),
        ("target", Jsonb(["one", "two", "three", "four", "five", "six", "seven"])),
        ("target", Jsonb([1])),
        ("target", Jsonb({"slug": "vip"})),
        ("target", Jsonb(None)),
        ("target", Jsonb([""])),
        ("target", Jsonb(["VIP"])),
    ]

    with psycopg.connect(sync_url) as connection:
        for pending_field, pending_options in valid_cases:
            _assert_insert(
                connection,
                statement,
                (uuid.uuid4(), pending_field, pending_options, expires_at),
                True,
            )
        for pending_field, pending_options in invalid_cases:
            _assert_insert(
                connection,
                statement,
                (uuid.uuid4(), pending_field, pending_options, expires_at),
                False,
            )


def test_search_cache_check_rejects_non_arrays_and_inconsistent_empty_flag(
    database_url: str,
) -> None:
    sync_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    statement = """
        INSERT INTO external_search_cache
            (cache_key, payload, is_empty, expires_at)
        VALUES (%s, %s, %s, %s)
    """
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    valid_cases = [
        (Jsonb([]), True),
        (Jsonb([{"slug": "vip"}]), False),
        (Jsonb([{"slug": "vip"}, {"slug": "short"}]), False),
    ]
    invalid_cases = [
        (Jsonb([]), False),
        (Jsonb([{"slug": "vip"}]), True),
        (Jsonb([{}, {}, {}]), False),
        (Jsonb({"slug": "vip"}), False),
        (Jsonb(1), False),
        (Jsonb(None), False),
    ]

    with psycopg.connect(sync_url) as connection:
        for payload, is_empty in valid_cases:
            _assert_insert(
                connection,
                statement,
                (f"phase1-{uuid.uuid4()}", payload, is_empty, expires_at),
                True,
            )
        for payload, is_empty in invalid_cases:
            _assert_insert(
                connection,
                statement,
                (f"phase1-{uuid.uuid4()}", payload, is_empty, expires_at),
                False,
            )


@pytest.mark.asyncio
async def test_conversation_state_none_roundtrip_is_sql_null(database_url: str) -> None:
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    state_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            state = ConversationState(
                id=state_id,
                pending_field=None,
                pending_options=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(state)
            await session.commit()
            result = await session.execute(
                sa.text(
                    "SELECT pending_field IS NULL, pending_options IS NULL "
                    "FROM conversation_states WHERE id = :id"
                ),
                {"id": state_id},
            )
            assert result.one() == (True, True)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM conversation_states WHERE id = :id"),
                {"id": state_id},
            )
        await engine.dispose()


def test_historical_throw_tables_are_not_a_runtime_seed_dependency(database_url: str) -> None:
    sync_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(sync_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('throw_methods', 'throw_method_aliases')
            ORDER BY table_name
            """
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "throw_method_aliases",
            "throw_methods",
        ]
