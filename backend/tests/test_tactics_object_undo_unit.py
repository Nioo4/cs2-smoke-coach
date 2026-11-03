"""Pure transaction-helper checks; no database, network or settings file."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.db.tactics_models import TacticOperation
from app.services import tactics_collaboration as collaboration


@pytest.mark.parametrize("case", ["valid", "null", "wrong_actor", "wrong_type", "missing_source",
                                  "wrong_tactic", "expired", "wrong_epoch", "wrong_deleter",
                                  "missing_recovery"])
async def test_undo_durable_source_and_recovery_rechecked(case):
    tactic, epoch, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source = SimpleNamespace(actor_id=user, type="stage.command")
    recovery = SimpleNamespace(tactic_id=tactic, source_epoch=epoch, deleted_by=user,
                               expires_at=datetime.now(UTC) + timedelta(days=1))
    if case == "wrong_actor":
        source.actor_id = uuid.uuid4()
    if case == "wrong_type":
        source.type = "text.update"
    if case == "missing_source":
        source = None
    if case == "wrong_tactic":
        recovery.tactic_id = uuid.uuid4()
    if case == "expired":
        recovery.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if case == "wrong_epoch":
        recovery.source_epoch = uuid.uuid4()
    if case == "wrong_deleter":
        recovery.deleted_by = uuid.uuid4()
    if case == "missing_recovery":
        recovery = None

    class Transaction:
        async def get(self, model, _key):
            return source if model is TacticOperation else recovery

    command = {"type": "object.undo", "source_operation_id": str(uuid.uuid4()),
               "recovery_id": None if case == "null" else str(uuid.uuid4())}
    if case in {"valid", "null"}:
        result = await collaboration.command_recovery(Transaction(), command, tactic, epoch, user)
        assert result is (None if case == "null" else recovery)
    else:
        with pytest.raises(AppError) as failure:
            await collaboration.command_recovery(Transaction(), command, tactic, epoch, user)
        assert failure.value.code == "TACTIC_CONFLICT"
