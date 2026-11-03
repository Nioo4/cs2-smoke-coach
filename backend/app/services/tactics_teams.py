"""Team operations run under the same permission-domain lock as document commits."""

import base64
import json
import uuid
from datetime import datetime

import sqlalchemy as sa

from app.core.errors import AppError
from app.db.tactics_models import (
    CollaborationGrant,
    CollaborationTicket,
    PersonalNotebook,
    Tactic,
    TacticDocument,
    TacticsSession,
    Team,
    TeamInvite,
    TeamInviteAcceptance,
    TeamMember,
)
from app.services import tactics_accounts as accounts
from app.services import tactics_documents as documents
from app.services import tactics_permissions as access


async def summary(db, team, user_id):
    notebook = await db.scalar(sa.select(PersonalNotebook).where(
        PersonalNotebook.team_id == team.id
    ))
    member = await db.get(TeamMember, (team.id, user_id))
    return {"id": str(team.id), "name": team.name, "owner_id": str(team.owner_id),
            "notebook_id": str(notebook.id),
            "my_role": "owner" if team.owner_id == user_id else member.role,
            "created_at": team.created_at.isoformat(), "updated_at": team.updated_at.isoformat(),
            "archived_at": team.archived_at.isoformat() if team.archived_at else None}


def member_view(member, user, owner_id):
    return {"user_id": str(user.id), "display_name": user.display_name,
            "role": "owner" if owner_id == user.id else member.role,
            "joined_at": member.joined_at.isoformat()}


def unavailable():
    return AppError(404, "INVITE_UNAVAILABLE", "邀请已失效，请联系队长获取新邀请。")


async def invite_access(db, user_id, token):
    # Only locate before locking; tokens are never returned or logged here.
    located = await db.scalar(sa.select(TeamInvite).where(
        TeamInvite.token_digest == accounts.digest(token)
    ))
    if located is None:
        raise unavailable()
    team = await db.scalar(sa.select(Team).where(Team.id == located.team_id).with_for_update())
    invite = await db.scalar(sa.select(TeamInvite).where(
        TeamInvite.id == located.id
    ).with_for_update().execution_options(populate_existing=True))
    member = await db.get(TeamMember, (team.id, user_id))
    accepted = await db.get(TeamInviteAcceptance, (invite.id, user_id))
    if team.status != "active" or invite.revoked_at or invite.expires_at <= accounts.now() or (
        invite.uses >= invite.max_uses and member is None
    ) or (accepted is not None and member is None):
        raise unavailable()
    return team, invite, member


async def revoke_connections(db, tactic_ids, user_id=None):
    if not tactic_ids:
        return
    at = accounts.now()
    grants = sa.update(CollaborationGrant).where(
        CollaborationGrant.tactic_id.in_(tactic_ids), CollaborationGrant.closed_at.is_(None)
    )
    tickets = sa.update(CollaborationTicket).where(
        CollaborationTicket.tactic_id.in_(tactic_ids), CollaborationTicket.consumed_at.is_(None)
    )
    if user_id:
        sessions = sa.select(TacticsSession.id).where(TacticsSession.user_id == user_id)
        grants = grants.where(CollaborationGrant.session_id.in_(sessions))
        tickets = tickets.where(CollaborationTicket.session_id.in_(sessions))
    await db.execute(grants.values(closed_at=at))
    await db.execute(tickets.values(consumed_at=at))


async def lock_team_documents(db, team_id):
    notebook = await db.scalar(sa.select(PersonalNotebook).where(
        PersonalNotebook.team_id == team_id
    ).with_for_update())
    tactics = (await db.scalars(sa.select(Tactic).where(
        Tactic.notebook_id == notebook.id
    ).order_by(Tactic.id).with_for_update())).all()
    docs = (await db.scalars(sa.select(TacticDocument.tactic_id).where(
        TacticDocument.tactic_id.in_([item.id for item in tactics])
    ).order_by(TacticDocument.tactic_id).with_for_update())).all()
    return notebook, tactics, docs


def page_query(query, model, cursor, binding, limit):
    if cursor:
        try:
            decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4),
                                                  altchars=b"-_", validate=True))
            if set(decoded) != {"b", "t", "i"} or decoded["b"] != binding:
                raise ValueError
            at = datetime.fromisoformat(decoded["t"])
            identity = uuid.UUID(decoded["i"])
            if at.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise documents.bad_input() from None
        query = query.where(sa.or_(model.created_at < at, sa.and_(
            model.created_at == at, model.id < identity
        )))
    return query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)


def next_cursor(rows, limit, binding):
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return base64.urlsafe_b64encode(documents.json_bytes({
        "b": binding, "t": last.created_at.isoformat(), "i": str(last.id),
    })).decode().rstrip("=")


async def copy_locks(db, user_id, source_id, target_id):
    source_notebook = await db.scalar(sa.select(Tactic.notebook_id).where(Tactic.id == source_id))
    if source_notebook is None:
        raise documents.missing()
    notebooks = await access.notebooks_access(db, user_id, [source_notebook, target_id])
    access.require(notebooks[target_id]._access_role, "create")
    tactic = await db.scalar(sa.select(Tactic).where(Tactic.id == source_id).with_for_update())
    doc = await db.scalar(sa.select(TacticDocument).where(
        TacticDocument.tactic_id == source_id
    ).with_for_update())
    if tactic.status != "active" or doc is None:
        raise documents.missing()
    return tactic, doc, notebooks[target_id]
