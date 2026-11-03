"""Only successful verified mutations, in the caller's transaction. No failure logger."""

import uuid

import sqlalchemy as sa

from app.db.tactics_models import AUDIT_ACTIONS, PersonalNotebook, Tactic, TacticAuditEvent


def request_id(request=None):
    if request is None:
        return uuid.uuid4().hex
    value = getattr(request.state, "_tactics_audit_id", None)
    if value is None:
        value = uuid.uuid4().hex
        request.state._tactics_audit_id = value
    return value


def identity(value):
    if value is None:
        return None
    normalized = uuid.UUID(str(value))
    if normalized.version != 4 or str(normalized) != str(value):
        raise ValueError("audit_identity_invalid")
    return normalized


async def append(db, action, actor_id, *, request=None, tactic_id=None, team_id=None,
                 resource_id=None, correlation=None, notebook_id=None):
    if action not in AUDIT_ACTIONS or not db.in_transaction():
        raise ValueError("audit_context_invalid")
    actor_id, tactic_id, team_id, resource_id = map(identity, (
        actor_id, tactic_id, team_id, resource_id))
    if notebook_id and team_id is None:
        team_id = await db.scalar(sa.select(PersonalNotebook.team_id).where(
            PersonalNotebook.id == identity(notebook_id)))
    if tactic_id and team_id is None:
        team_id = await db.scalar(sa.select(PersonalNotebook.team_id).join(
            Tactic, Tactic.notebook_id == PersonalNotebook.id).where(Tactic.id == tactic_id))
    value = correlation if correlation is not None else request_id(request)
    if not isinstance(value, str) or len(value) != 32 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise ValueError("audit_correlation_invalid")
    db.add(TacticAuditEvent(actor_id=actor_id, tactic_id=tactic_id, team_id=team_id,
                           resource_id=resource_id, action=action, result_code="SUCCESS",
                           request_id=value))
    await db.flush()
