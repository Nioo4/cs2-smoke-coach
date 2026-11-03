"""Shared permission-domain locking for REST and Node grants."""

from datetime import timedelta

import sqlalchemy as sa

from app.core.errors import AppError
from app.db.tactics_models import PersonalNotebook, Team, TeamMember
from app.services.tactics_accounts import now


def missing():
    return AppError(404, "TACTIC_NOT_FOUND", "资源不存在。")


def require(role, capability):
    allowed = role == "owner" or capability == "read" or (
        role == "editor" and capability in {"create", "edit"}
    )
    if not allowed:
        raise AppError(403, "USER_FORBIDDEN", "没有此操作权限。")


def permissions(role, *, active=True, notebook=False):
    return {"read": True, "create" if notebook else "edit": active and role != "viewer",
            "manage": role == "owner", "publish": active and role == "owner"}


async def team_access(db, user_id, team_id, *, archived=False, capability="read"):
    team = await db.scalar(sa.select(Team).where(Team.id == team_id).with_for_update()
                           .execution_options(populate_existing=True))
    member = await db.get(TeamMember, (team_id, user_id)) if team else None
    if team is None or member is None or (team.status != "active" and not archived) or (
        team.status == "archived" and (team.owner_id != user_id or
                                      team.archived_at <= now() - timedelta(days=30))
    ):
        raise missing()
    role = "owner" if team.owner_id == user_id else member.role
    require(role, capability)
    return team, role


async def notebooks_access(db, user_id, notebook_ids, *, capability="read"):
    """Lock every domain before every notebook, including cross-notebook copies."""
    identities = sorted(set(notebook_ids))
    rows = (await db.scalars(sa.select(PersonalNotebook).where(
        PersonalNotebook.id.in_(identities)
    ))).all()
    if len(rows) != len(identities):
        raise missing()
    team_roles = {}
    for identity in sorted({row.team_id for row in rows if row.team_id is not None}):
        _, team_roles[identity] = await team_access(db, user_id, identity)
    result = {}
    for row in sorted(rows, key=lambda item: item.id):
        locked = await db.scalar(sa.select(PersonalNotebook).where(
            PersonalNotebook.id == row.id
        ).with_for_update())
        if locked.kind == "personal":
            if locked.user_id != user_id:
                raise missing()
            role = "owner"
        else:
            role = team_roles[locked.team_id]
        require(role, capability)
        locked._access_role = role
        result[locked.id] = locked
    return result


def notebook_view(notebook):
    return {"id": str(notebook.id), "kind": notebook.kind,
            "user_id": str(notebook.user_id) if notebook.user_id else None,
            "team_id": str(notebook.team_id) if notebook.team_id else None}
