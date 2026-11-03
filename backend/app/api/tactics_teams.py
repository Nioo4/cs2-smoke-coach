"""Private team directory, roles, invitations and cross-notebook copies."""

import secrets
import uuid
from datetime import timedelta
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from pydantic import StringConstraints

from app.api.tactics_auth import AccountRoute, response
from app.core.errors import AppError
from app.db.tactics_models import (
    PersonalNotebook,
    Tactic,
    TacticDocument,
    TacticsUser,
    Team,
    TeamInvite,
    TeamInviteAcceptance,
    TeamMember,
)
from app.schemas.tactics_auth import EmptyBody
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit
from app.services import tactics_documents as documents
from app.services import tactics_history as history
from app.services import tactics_permissions as access
from app.services import tactics_shares as shares
from app.services import tactics_teams as teams

router = APIRouter(prefix="/api/v1", route_class=AccountRoute, tags=["tactics-teams"])
Limit = Annotated[int, Query(ge=1, le=50)]
Cursor = Annotated[str | None, Query(max_length=2048)]


class TeamBody(EmptyBody):
    name: Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                          min_length=1, max_length=100)]


class TokenBody(EmptyBody):
    token: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$")]


class RoleBody(EmptyBody):
    role: Literal["viewer", "editor"]


class OwnershipBody(EmptyBody):
    user_id: uuid.UUID


class CopyBody(EmptyBody):
    target_notebook_id: uuid.UUID


async def listing(request, archived, limit, cursor):
    async with accounts.authenticated(request) as (db, user, _, _):
        query = sa.select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(
            TeamMember.user_id == user.id, Team.status == ("archived" if archived else "active")
        )
        if archived:
            query = query.where(Team.owner_id == user.id,
                                Team.archived_at > accounts.now() - timedelta(days=30))
        binding = [str(user.id), archived]
        rows = (await db.scalars(teams.page_query(query, Team, cursor, binding, limit))).all()
        visible = set()
        # Domain locks use the same UUID order as copies, even when directory sort differs.
        for item in sorted(rows[:limit], key=lambda team: team.id):
            try:
                await access.team_access(db, user.id, item.id, archived=archived)
                visible.add(item.id)
            except AppError as exc:
                if exc.status_code != 404:
                    raise
        result = {"items": [await teams.summary(db, item, user.id) for item in rows[:limit]
                            if item.id in visible],
                  "next_cursor": teams.next_cursor(rows, limit, binding)}
    return response(result)


@router.get("/teams")
async def list_teams(request: Request, limit: Limit = 20, cursor: Cursor = None):
    return await listing(request, False, limit, cursor)


@router.get("/teams/archived")
async def list_archived(request: Request, limit: Limit = 20, cursor: Cursor = None):
    return await listing(request, True, limit, cursor)


@router.post("/teams", status_code=201)
async def create_team(body: TeamBody, request: Request):
    key = documents.idempotency_key(request)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        prior = await documents.lookup_idempotency(
            db, user.id, "team:create", key, body.model_dump())
        if prior:
            team, _ = await access.team_access(db, user.id, prior.resource_id, capability="manage")
            notebook = await db.scalar(sa.select(PersonalNotebook).where(
                PersonalNotebook.team_id == team.id
            ))
        else:
            team = Team(name=body.name, owner_id=user.id)
            db.add(team)
            await db.flush()
            db.add(TeamMember(team_id=team.id, user_id=user.id, role="editor"))
            notebook = PersonalNotebook(kind="team", user_id=None, team_id=team.id)
            db.add(notebook)
            await audit.append(db, "team.create", user.id, request=request,
                               team_id=team.id, resource_id=team.id)
            documents.remember_idempotency(db, user.id, "team:create", key,
                                           body.model_dump(), team.id)
            await db.flush()
        result = {"team": await teams.summary(db, team, user.id),
                  "notebook": access.notebook_view(notebook)}
    return response(result, status=201)


@router.get("/teams/{team_id}")
async def get_team(team_id: uuid.UUID, request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        team, role = await access.team_access(db, user.id, team_id)
        notebook = await db.scalar(sa.select(PersonalNotebook).where(
            PersonalNotebook.team_id == team.id
        ))
        rows = (await db.execute(sa.select(TeamMember, TacticsUser).join(
            TacticsUser, TacticsUser.id == TeamMember.user_id
        ).where(TeamMember.team_id == team.id).order_by(TeamMember.joined_at,
                                                       TeamMember.user_id))).all()
        result = {"team": await teams.summary(db, team, user.id), "my_role": role,
                  "notebook": access.notebook_view(notebook),
                  "members": [teams.member_view(member, actor, team.owner_id)
                              for member, actor in rows],
                  "permissions": access.permissions(role, notebook=True)}
    return response(result)


@router.patch("/teams/{team_id}")
async def rename(team_id: uuid.UUID, body: TeamBody, request: Request):
    key = documents.idempotency_key(request)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, _ = await access.team_access(db, user.id, team_id, capability="manage")
        scope = f"team:{team_id}:rename"
        prior = await documents.lookup_idempotency(db, user.id, scope, key, body.model_dump())
        if not prior:
            changed = team.name != body.name
            team.name, team.updated_at = body.name, accounts.now()
            if changed:
                await audit.append(db, "team.rename", user.id, request=request,
                                   team_id=team.id, resource_id=team.id)
            documents.remember_idempotency(db, user.id, scope, key, body.model_dump(), team.id)
        result = {"team": await teams.summary(db, team, user.id)}
    return response(result)


@router.get("/teams/{team_id}/invites")
async def list_invites(team_id: uuid.UUID, request: Request,
                       limit: Limit = 20, cursor: Cursor = None):
    async with accounts.authenticated(request) as (db, user, _, _):
        await access.team_access(db, user.id, team_id, capability="manage")
        binding = [str(user.id), str(team_id)]
        rows = (await db.scalars(teams.page_query(sa.select(TeamInvite).where(
            TeamInvite.team_id == team_id
        ), TeamInvite, cursor, binding, limit))).all()
        result = {"items": [{"invite_id": str(item.id), "created_at": item.created_at.isoformat(),
                             "expires_at": item.expires_at.isoformat(), "max_uses": item.max_uses,
                             "used_count": item.uses,
                             "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None}
                            for item in rows[:limit]],
                  "next_cursor": teams.next_cursor(rows, limit, binding)}
    return response(result)


@router.post("/teams/{team_id}/invites", status_code=201)
async def create_invite(team_id: uuid.UUID, body: EmptyBody, request: Request):
    key = documents.idempotency_key(request)
    scope, token = f"team:{team_id}:invite", None
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        await access.team_access(db, user.id, team_id, capability="manage")
        prior = await documents.lookup_idempotency(db, user.id, scope, key, {})
        if prior:
            invite = await db.get(TeamInvite, prior.resource_id)
        else:
            token = secrets.token_urlsafe(32)
            invite = TeamInvite(team_id=team_id, token_digest=accounts.digest(token),
                                created_by=user.id, expires_at=accounts.now() + timedelta(days=7))
            db.add(invite)
            await db.flush()
            await audit.append(db, "team.invite.create", user.id, request=request,
                               team_id=team_id, resource_id=invite.id)
            documents.remember_idempotency(db, user.id, scope, key, {}, invite.id)
        result = {"invite_id": str(invite.id), "token": token, "token_available": token is not None,
                  "expires_at": invite.expires_at.isoformat(), "max_uses": invite.max_uses}
    return response(result, status=201 if token else 200)


@router.post("/teams/{team_id}/invites/{invite_id}/revoke")
async def revoke_invite(team_id: uuid.UUID, invite_id: uuid.UUID,
                        body: EmptyBody, request: Request):
    key = documents.idempotency_key(request)
    scope = f"team:{team_id}:invite:{invite_id}:revoke"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        await access.team_access(db, user.id, team_id, capability="manage")
        invite = await db.get(TeamInvite, invite_id)
        if invite is None or invite.team_id != team_id:
            raise documents.missing()
        if not await documents.lookup_idempotency(db, user.id, scope, key, {}):
            if invite.revoked_at is None:
                invite.revoked_at = accounts.now()
                await audit.append(db, "team.invite.revoke", user.id, request=request,
                                   team_id=team_id, resource_id=invite.id)
            documents.remember_idempotency(db, user.id, scope, key, {}, invite.id)
    return response({"ok": True})


@router.post("/invites/preview")
async def preview_invite(body: TokenBody, request: Request):
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, invite, member = await teams.invite_access(db, user.id, body.token)
        result = {"team_id": str(team.id), "team_name": team.name,
                  "expires_at": invite.expires_at.isoformat(), "already_member": member is not None}
    return response(result)


@router.post("/invites/accept")
async def accept_invite(body: TokenBody, request: Request):
    key = documents.idempotency_key(request)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, invite, member = await teams.invite_access(db, user.id, body.token)
        scope = "invite:accept"
        # Never persist the raw invitation token in an idempotency body/result.
        payload = {"invite_id": str(invite.id)}
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        changed = member is None
        if member is None:
            count = await db.scalar(sa.select(sa.func.count()).select_from(TeamMember).where(
                TeamMember.team_id == team.id
            ))
            if count >= 20:
                raise AppError(409, "TEAM_MEMBER_LIMIT_REACHED", "队伍最多20名成员。")
            member = TeamMember(team_id=team.id, user_id=user.id, role="viewer")
            db.add(member)
            invite.uses += 1
            await db.flush()
        if await db.get(TeamInviteAcceptance, (invite.id, user.id)) is None:
            db.add(TeamInviteAcceptance(invite_id=invite.id, user_id=user.id))
            changed = True
        if changed:
            await audit.append(db, "team.invite.accept", user.id, request=request,
                               team_id=team.id, resource_id=invite.id)
        if not prior:
            documents.remember_idempotency(db, user.id, scope, key, payload, team.id)
        role = "owner" if team.owner_id == user.id else member.role
    return response({"team_id": str(team.id), "member_role": role})


@router.patch("/teams/{team_id}/members/{user_id}")
async def change_member(team_id: uuid.UUID, user_id: uuid.UUID,
                        body: RoleBody, request: Request):
    key = documents.idempotency_key(request)
    scope = f"team:{team_id}:member:{user_id}:role"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, _ = await access.team_access(db, user.id, team_id, capability="manage")
        target = await db.get(TeamMember, (team_id, user_id))
        if target is None:
            raise documents.missing()
        if user_id == team.owner_id:
            raise AppError(409, "TEAM_OWNER_REQUIRED", "请使用队长转让操作。")
        prior = await documents.lookup_idempotency(db, user.id, scope, key, body.model_dump())
        if not prior:
            _, tactics, _ = await teams.lock_team_documents(db, team_id)
            changed = target.role != body.role
            target.role = body.role
            await teams.revoke_connections(db, [item.id for item in tactics], user_id)
            if changed:
                await audit.append(db, "team.member.role", user.id, request=request,
                                   team_id=team_id, resource_id=user_id)
            documents.remember_idempotency(db, user.id, scope, key, body.model_dump(), team_id)
        actor = await db.get(TacticsUser, user_id)
        result = {"member": teams.member_view(target, actor, team.owner_id)}
    return response(result)


@router.delete("/teams/{team_id}/members/{user_id}")
async def remove_member(team_id: uuid.UUID, user_id: uuid.UUID, request: Request):
    key = documents.idempotency_key(request)
    scope = f"team:{team_id}:member:{user_id}:remove"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, role = await access.team_access(db, user.id, team_id)
        if user.id != user_id:
            access.require(role, "manage")
        if user_id == team.owner_id:
            raise AppError(409, "TEAM_OWNER_REQUIRED", "队长须先转让或归档队伍。")
        target = await db.get(TeamMember, (team_id, user_id))
        prior = await documents.lookup_idempotency(db, user.id, scope, key, {})
        if not prior:
            if target is None:
                raise documents.missing()
            _, tactics, _ = await teams.lock_team_documents(db, team_id)
            await db.delete(target)
            await teams.revoke_connections(db, [item.id for item in tactics], user_id)
            action = "team.member.leave" if user.id == user_id else "team.member.remove"
            await audit.append(db, action, user.id, request=request,
                               team_id=team_id, resource_id=user_id)
            documents.remember_idempotency(db, user.id, scope, key, {}, team_id)
    return response({"ok": True})


@router.post("/teams/{team_id}/ownership")
async def transfer(team_id: uuid.UUID, body: OwnershipBody, request: Request):
    key = documents.idempotency_key(request)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, _ = await access.team_access(db, user.id, team_id, capability="manage")
        target = await db.get(TeamMember, (team_id, body.user_id))
        if target is None or body.user_id == user.id:
            raise documents.bad_input()
        scope, payload = f"team:{team_id}:owner", {"user_id": str(body.user_id)}
        if not await documents.lookup_idempotency(db, user.id, scope, key, payload):
            original = await db.get(TeamMember, (team_id, user.id))
            original.role, target.role = "editor", "editor"
            team.owner_id, team.updated_at = body.user_id, accounts.now()
            await audit.append(db, "team.transfer", user.id, request=request,
                               team_id=team_id, resource_id=body.user_id)
            documents.remember_idempotency(db, user.id, scope, key, payload, team.id)
        result = {"team": await teams.summary(db, team, user.id)}
    return response(result)


@router.post("/teams/{team_id}/archive")
async def archive(team_id: uuid.UUID, body: EmptyBody, request: Request):
    key = documents.idempotency_key(request)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, _ = await access.team_access(db, user.id, team_id, archived=True, capability="manage")
        scope = f"team:{team_id}:archive"
        if not await documents.lookup_idempotency(db, user.id, scope, key, {}):
            notebook, tactics, docs = await teams.lock_team_documents(db, team_id)
            at = accounts.now()
            team.status, team.archived_at, team.updated_at = "archived", team.archived_at or at, at
            for identity in docs:
                doc = await db.get(TacticDocument, identity)
                await history.save_history(db, doc, user.id, "archive")
                doc.epoch, doc.updated_at = uuid.uuid4(), at
                await db.flush()
                db.expunge(doc)
            await shares.revoke_sources(db, notebook.id)
            await teams.revoke_connections(db, [item.id for item in tactics])
            await db.execute(sa.update(TeamInvite).where(
                TeamInvite.team_id == team_id, TeamInvite.revoked_at.is_(None)
            ).values(revoked_at=at))
            await audit.append(db, "team.archive", user.id, request=request,
                               team_id=team_id, resource_id=team_id)
            documents.remember_idempotency(db, user.id, scope, key, {}, team.id)
        result = {"team_id": str(team.id), "archived_at": team.archived_at.isoformat()}
    return response(result)


@router.post("/teams/{team_id}/restore")
async def restore(team_id: uuid.UUID, body: EmptyBody, request: Request):
    key = documents.idempotency_key(request)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        team, _ = await access.team_access(db, user.id, team_id, archived=True, capability="manage")
        scope = f"team:{team_id}:restore"
        if not await documents.lookup_idempotency(db, user.id, scope, key, {}):
            changed = team.status != "active"
            team.status, team.archived_at, team.updated_at = "active", None, accounts.now()
            if changed:
                await audit.append(db, "team.restore", user.id, request=request,
                                   team_id=team_id, resource_id=team_id)
            documents.remember_idempotency(db, user.id, scope, key, {}, team.id)
        result = {"team": await teams.summary(db, team, user.id)}
    return response(result)


@router.post("/tactics/{tactic_id}/copy", status_code=201)
async def copy_tactic(tactic_id: uuid.UUID, body: CopyBody, request: Request):
    key = documents.idempotency_key(request)
    scope = f"tactic:{tactic_id}:copy"
    payload = {"target_notebook_id": str(body.target_notebook_id)}
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        source, doc, target = await teams.copy_locks(
            db, user.id, tactic_id, body.target_notebook_id)
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            copied, stored = await documents.owned_tactic(db, user.id, prior.resource_id)
            return response(documents.summary(copied, stored), status=201)
        await documents.check_quota(db, target.id)
        expected, projection = (doc.epoch, doc.revision), doc.projection
        creator = await db.get(TacticsUser, source.created_by)
        attribution = {"tactic_id": str(source.id), "title": source.title,
                       "author_display_name": creator.display_name,
                       "copied_at": accounts.now().isoformat()}
    identity = uuid.uuid4()
    state, projection, digest, projection_digest = await documents.request_copy(
        identity, projection)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        source, doc, target = await teams.copy_locks(
            db, user.id, tactic_id, body.target_notebook_id)
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            copied, stored = await documents.owned_tactic(db, user.id, prior.resource_id)
        else:
            if (doc.epoch, doc.revision) != expected:
                raise AppError(409, "TACTIC_CONFLICT", "来源已变化，请重新复制。")
            await documents.check_quota(db, target.id)
            copied = Tactic(id=identity, notebook_id=target.id, created_by=user.id,
                            title=source.title, side=source.side, source_attribution=attribution)
            db.add(copied)
            await db.flush()
            stored = TacticDocument(tactic_id=identity, yjs_state=state, projection=projection,
                                    state_digest=digest, projection_digest=projection_digest)
            db.add(stored)
            await audit.append(db, "tactic.copy", user.id, request=request,
                               tactic_id=copied.id, team_id=target.team_id, resource_id=source.id)
            documents.remember_idempotency(db, user.id, scope, key, payload, identity)
            await db.flush()
        copied._access_role = target._access_role
        result = documents.summary(copied, stored)
    return response(result, status=201)
