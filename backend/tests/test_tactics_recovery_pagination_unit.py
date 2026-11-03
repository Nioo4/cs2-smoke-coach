"""Pure recovery-route/query/cursor checks; no HTTP transport, settings, DB or Node."""

import base64
import inspect
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_type_hints

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects import postgresql

from app.api import tactics_collaboration as api
from app.core.errors import AppError


@pytest.fixture
def harness(monkeypatch):
    at = datetime(2026, 10, 30, tzinfo=UTC)
    context = SimpleNamespace(
        user_id=uuid.uuid4(), tactic_id=uuid.uuid4(), notebook_id=uuid.uuid4(),
        epoch=uuid.uuid4(), role="editor", at=at, denied=None,
    )
    rows = [SimpleNamespace(id=uuid.UUID(int=index), kind="action", entity_id=uuid.uuid4(),
                            created_at=at, expires_at=at + timedelta(days=1))
            for index in [3, 2, 1]]
    executed = []

    class Database:
        async def execute(self, query):
            executed.append(query)
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def authenticated(_request):
        yield Database(), SimpleNamespace(id=context.user_id), None, None

    async def owned(_db, user_id, tactic_id, *, capability):
        assert user_id == context.user_id and tactic_id == context.tactic_id
        assert capability == "edit"
        if context.denied:
            raise AppError(context.denied, "USER_FORBIDDEN", "denied fixture")
        return SimpleNamespace(notebook_id=context.notebook_id, _access_role=context.role), (
            SimpleNamespace(epoch=context.epoch)
        )

    monkeypatch.setattr(api.accounts, "authenticated", authenticated)
    monkeypatch.setattr(api.documents, "owned_tactic", owned)
    monkeypatch.setattr(api.accounts, "now", lambda: context.at)
    return context, rows, executed


async def listing(context, **params):
    return await api.recovery_list(context.tactic_id, object(), **params)


def body(response):
    assert response.headers["cache-control"] == "no-store"
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_recovery_page_reuses_keyset_helper_and_selects_only_summary_metadata(harness):
    context, rows, queries = harness
    result = body(await listing(context, limit=2))
    assert [item["id"] for item in result["items"]] == [str(row.id) for row in rows[:2]]
    assert set(result["items"][0]) == {"id", "kind", "entity_id", "created_at", "expires_at"}
    decoded = json.loads(base64.urlsafe_b64decode(result["next_cursor"] + "==="))
    assert decoded == {"b": ["tactic-recoveries", str(context.user_id), str(context.tactic_id),
                             str(context.notebook_id), "editor", str(context.epoch), "unexpired"],
                       "t": context.at.isoformat(), "i": str(rows[1].id)}
    query = queries[0]
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "created_at DESC, tactic_recovery_items.id DESC" in sql
    assert query._limit_clause.value == 3
    assert query.compile().params["expires_at_1"] == context.at
    assert {column.key for column in query.selected_columns} == {
        "id", "kind", "entity_id", "created_at", "expires_at"
    }
    assert "snapshot" not in sql and "texts" not in sql


@pytest.mark.asyncio
async def test_cursor_has_time_and_uuid_tie_breaker_and_expiry_is_rechecked_each_page(harness):
    context, rows, queries = harness
    cursor = body(await listing(context, limit=1))["next_cursor"]
    context.at += timedelta(seconds=5)
    rows.clear()
    assert body(await listing(context, limit=1, cursor=cursor)) == {
        "items": [], "next_cursor": None,
    }
    compiled = queries[-1].compile(dialect=postgresql.dialect())
    assert "created_at <" in str(compiled) and "tactic_recovery_items.id <" in str(compiled)
    assert "created_at =" in str(compiled)
    assert compiled.params["expires_at_1"] == context.at
    assert compiled.params["id_1"] == uuid.UUID(int=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_id", "tactic_id", "notebook_id", "epoch", "role"])
async def test_cursor_cannot_cross_current_resource_or_authorization_context(harness, field):
    context, _, queries = harness
    cursor = body(await listing(context, limit=1))["next_cursor"]
    setattr(context, field, "owner" if field == "role" else uuid.uuid4())
    queries.clear()
    with pytest.raises(AppError) as error:
        await listing(context, limit=1, cursor=cursor)
    assert error.value.code == "VALIDATION_ERROR" and not queries


@pytest.mark.asyncio
async def test_cursor_binding_cannot_switch_to_an_expired_filter(harness):
    context, _, queries = harness
    cursor = body(await listing(context, limit=1))["next_cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "==="))
    decoded["b"][-1] = "expired"
    changed = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    queries.clear()
    with pytest.raises(AppError) as error:
        await listing(context, cursor=changed)
    assert error.value.code == "VALIDATION_ERROR" and not queries


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["not base64", "W10", "e30"])
async def test_malformed_cursor_is_rejected_before_query_execution(harness, cursor):
    context, _, queries = harness
    with pytest.raises(AppError) as error:
        await listing(context, cursor=cursor)
    assert error.value.status_code == 422 and not queries


@pytest.mark.asyncio
async def test_authorization_is_rechecked_before_reading_cursor_or_recoveries(harness):
    context, _, queries = harness
    context.denied = 403
    with pytest.raises(AppError) as error:
        await listing(context, cursor="not base64")
    assert error.value.status_code == 403 and not queries


def test_route_parameter_contract_matches_general_directory_limits():
    hints = get_type_hints(api.recovery_list, include_extras=True)
    limit = TypeAdapter(hints["limit"])
    assert inspect.signature(api.recovery_list).parameters["limit"].default == 20
    assert limit.validate_python("1") == 1 and limit.validate_python("100") == 100
    for value in [0, 101, "bad", 1.5]:
        with pytest.raises(ValidationError):
            limit.validate_python(value)
    cursor = TypeAdapter(hints["cursor"])
    assert cursor.validate_python(None) is None
    assert cursor.validate_python("x" * 2048) == "x" * 2048
    with pytest.raises(ValidationError):
        cursor.validate_python("x" * 2049)
