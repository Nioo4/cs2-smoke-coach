"""Pure audit/metadata/TOC checks; no database, HTTP, Docker or settings file."""

import importlib.util
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.api import tactics as tactics_api
from app.db.tactics_models import AUDIT_ACTIONS, TacticAuditEvent
from app.schemas.tactics_auth import EmptyBody
from app.services import tactics_audit as audit
from app.services import tactics_history as history


class Transaction:
    def __init__(self, fail=False):
        self.rows, self.fail = [], fail

    def in_transaction(self):
        return True

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        if self.fail:
            raise RuntimeError("injected_audit_insert_failure")


async def test_safe_event_has_only_approved_metadata_and_server_request_identity():
    request = SimpleNamespace(state=SimpleNamespace(request_id="f" * 32),
                              headers={"x-request-id": "f" * 32})
    first = audit.request_id(request)
    assert first != "f" * 32 and first == audit.request_id(request)
    db = Transaction()
    actor, resource = uuid.uuid4(), uuid.uuid4()
    await audit.append(db, "account.login", actor, request=request, resource_id=resource)
    event = db.rows[0]
    assert event.actor_id == actor and event.resource_id == resource
    assert event.result_code == "SUCCESS" and event.request_id == first
    assert set(event.__table__.columns.keys()) == {
        "id", "actor_id", "tactic_id", "team_id", "resource_id", "action",
        "result_code", "request_id", "created_at"
    }
    for name in ["password", "csrf", "cookie", "payload", "username", "token", "ip"]:
        assert name not in event.__table__.columns


async def test_invalid_context_and_insert_errors_never_become_success():
    db = Transaction()
    with pytest.raises(ValueError, match="audit_context_invalid"):
        await audit.append(db, "arbitrary_user_text", uuid.uuid4())
    with pytest.raises(ValueError, match="audit_identity_invalid"):
        await audit.append(db, "account.login", str(uuid.uuid4()).upper())
    with pytest.raises(ValueError, match="audit_correlation_invalid"):
        await audit.append(db, "account.login", uuid.uuid4(), correlation="external secret")
    assert not db.rows
    failing = Transaction(fail=True)
    with pytest.raises(RuntimeError, match="injected_audit_insert_failure"):
        await audit.append(failing, "account.login", uuid.uuid4())


def test_frozen_migration_actions_and_fk_delete_safety():
    path = Path(__file__).parents[1] / "alembic/versions/0011_tactics_audit_events.py"
    spec = importlib.util.spec_from_file_location("audit_0011", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.ACTIONS) == AUDIT_ACTIONS
    assert module.down_revision == "0010_tactics_shares_history"
    ddl = str(CreateTable(TacticAuditEvent.__table__).compile(dialect=postgresql.dialect()))
    assert ddl.count("ON DELETE SET NULL") == 3
    assert "result_code='SUCCESS'" in ddl and "^[0-9a-f]{32}$" in ddl





@pytest.mark.parametrize("prior,actor", [(True, None), (False, None), (False, uuid.uuid4())])
async def test_periodic_new_version_uses_verified_actor_or_null(monkeypatch, prior, actor):
    calls = []

    class HistoryDB(Transaction):
        async def execute(self, *_):
            return None

        async def scalar(self, *_):
            return SimpleNamespace(id=uuid.uuid4()) if prior else None

        def add(self, row):
            row.id = uuid.uuid4()
            self.rows.append(row)

    async def observed(_db, action, user, **kwargs):
        calls.append((action, user))

    monkeypatch.setattr(audit, "append", observed)
    document = SimpleNamespace(tactic_id=uuid.uuid4(), epoch=uuid.uuid4(), revision=1,
                               yjs_state=b"state", projection={})
    await history.save_history(HistoryDB(), document, uuid.uuid4(), "periodic",
                               audit_actor_id=actor)
    assert calls == ([] if prior else [("history.checkpoint", actor)])


@pytest.mark.parametrize("status,prior", [
    ("active", False), ("archived", False), ("archived", True)
])
async def test_actual_restore_route_records_only_material_change(monkeypatch, status, prior):
    db = Transaction()
    user = SimpleNamespace(id=uuid.uuid4())
    tactic = SimpleNamespace(id=uuid.uuid4(), notebook_id=uuid.uuid4(), status=status)
    document = SimpleNamespace(revision=7)
    calls = []

    @asynccontextmanager
    async def authenticated(*_args, **_kwargs):
        yield db, user, None, None

    async def owned(*_args, **_kwargs):
        return tactic, document

    async def lookup(*_args):
        return SimpleNamespace(result={"revision": 7}) if prior else None

    async def quota(*_args):
        return None

    async def observed(*_args, **_kwargs):
        calls.append("tactic.restore")

    monkeypatch.setattr(tactics_api.accounts, "authenticated", authenticated)
    monkeypatch.setattr(tactics_api.documents, "owned_tactic", owned)
    monkeypatch.setattr(tactics_api.documents, "lookup_idempotency", lookup)
    monkeypatch.setattr(tactics_api.documents, "check_quota", quota)
    monkeypatch.setattr(tactics_api.documents, "summary",
                        lambda *_: {"revision": document.revision})
    monkeypatch.setattr(tactics_api.documents, "remember_idempotency", lambda *_: None)
    monkeypatch.setattr(audit, "append", observed)
    request = SimpleNamespace(headers={"idempotency-key": str(uuid.uuid4())})
    await tactics_api.restore_tactic(tactic.id, EmptyBody(), request)
    assert calls == (["tactic.restore"] if status == "archived" and not prior else [])
    assert document.revision == (8 if calls else 7)
