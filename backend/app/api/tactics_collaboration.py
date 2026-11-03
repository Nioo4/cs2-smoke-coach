"""Single-use collaboration tickets and private Node persistence endpoints."""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from pydantic import Field, StrictInt, StrictStr

from app.api.tactics_auth import AccountRoute, response
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.routing import JsonBodyRoute
from app.db.tactics_models import CollaborationGrant, CollaborationTicket, TacticRecoveryItem
from app.schemas.tactics_auth import EmptyBody
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit
from app.services import tactics_collaboration as collaboration
from app.services import tactics_documents as documents
from app.services import tactics_permissions as access
from app.services import tactics_teams as teams


class InternalRoute(JsonBodyRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def checked(request: Request):
            settings = get_settings()
            if not settings.tactics_enabled:
                raise AppError(404, "ENDPOINT_NOT_FOUND", "接口不存在。")
            key = settings.collab_internal_key.get_secret_value()
            if len(key.encode()) < 32 or not hmac.compare_digest(
                accounts.digest(request.headers.get("internal-key", "")), accounts.digest(key)
            ):
                raise AppError(401, "USER_AUTH_REQUIRED", "内部认证无效。")
            if request.query_params:
                raise documents.bad_input()
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 12 * 1024 * 1024:
                    raise AppError(413, "REQUEST_TOO_LARGE", "请求内容过大。")
            request._body = bytes(chunks)
            return await original(request)

        return checked


router = APIRouter(prefix="/api/v1", route_class=AccountRoute, tags=["tactics-collaboration"])
internal_router = APIRouter(prefix="/api/v1/internal/tactics", route_class=InternalRoute,
                            tags=["internal-collaboration"])


class ConsumeBody(EmptyBody):
    ticket: Annotated[StrictStr, Field(min_length=43, max_length=43)]
    connection_id: uuid.UUID
    node_instance_id: uuid.UUID


class GrantBody(EmptyBody):
    grant_id: uuid.UUID


class CheckBody(EmptyBody):
    grant_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=100)]


class LookupBody(GrantBody):
    epoch: uuid.UUID
    operation_id: uuid.UUID


class CommitBody(LookupBody):
    type: Literal["text.update", "stage.command"]
    expected_revision: Annotated[StrictInt, Field(ge=0, le=9007199254740991)]
    original_payload: dict
    payload_json: StrictStr
    fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    state_base64: Annotated[StrictStr, Field(max_length=5592408)]
    projection: dict
    projection_json: StrictStr
    state_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    projection_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


@router.post("/tactics/{tactic_id}/collaboration-ticket", status_code=201)
async def ticket(tactic_id: uuid.UUID, body: EmptyBody, request: Request):
    if len(get_settings().collab_internal_key.get_secret_value().encode()) < 32:
        raise AppError(503, "COLLAB_UNAVAILABLE", "协作服务暂不可用。")
    value = secrets.token_urlsafe(32)
    async with accounts.authenticated(request, write=True) as (db, user, session, _):
        tactic, _ = await documents.owned_tactic(db, user.id, tactic_id)
        role = "viewer" if tactic._access_role == "viewer" else "editor"
        db.add(CollaborationTicket(ticket_digest=accounts.digest(value), session_id=session.id,
                                   tactic_id=tactic_id,
                                   expires_at=accounts.now() + timedelta(seconds=60)))
    return response({"ticket": value, "expires_in": 60, "role": role,
                     "read_only": role == "viewer"},
                    status=201)


@router.get("/tactics/{tactic_id}/recoveries")
async def recovery_list(
    tactic_id: uuid.UUID, request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    async with accounts.authenticated(request) as (db, user, _, _):
        tactic, document = await documents.owned_tactic(db, user.id, tactic_id, capability="edit")
        binding = ["tactic-recoveries", str(user.id), str(tactic_id), str(tactic.notebook_id),
                   tactic._access_role, str(document.epoch), "unexpired"]
        query = sa.select(
            TacticRecoveryItem.id, TacticRecoveryItem.kind, TacticRecoveryItem.entity_id,
            TacticRecoveryItem.created_at, TacticRecoveryItem.expires_at,
        ).where(TacticRecoveryItem.tactic_id == tactic_id,
                TacticRecoveryItem.expires_at > accounts.now())
        rows = (await db.execute(teams.page_query(
            query, TacticRecoveryItem, cursor, binding, limit
        ))).all()
        result = {"items": [{"id": str(item.id), "kind": item.kind,
                             "entity_id": str(item.entity_id),
                             "created_at": item.created_at.isoformat(),
                             "expires_at": item.expires_at.isoformat()}
                            for item in rows[:limit]],
                  "next_cursor": teams.next_cursor(rows, limit, binding)}
    return response(result)


@internal_router.post("/tickets/consume")
async def consume(body: ConsumeBody):
    async with accounts.get_session_factory()() as db, db.begin():
        session_id = await db.scalar(sa.select(CollaborationTicket.session_id).where(
            CollaborationTicket.ticket_digest == accounts.digest(body.ticket)
        ))
        if not session_id:
            raise collaboration.denied()
        user, session = await collaboration.lock_identity(db, session_id)
        located = await db.scalar(sa.select(CollaborationTicket).where(
            CollaborationTicket.ticket_digest == accounts.digest(body.ticket)
        ))
        tactic, _ = await documents.owned_tactic(db, user.id, located.tactic_id)
        saved = await db.scalar(sa.select(CollaborationTicket).where(
            CollaborationTicket.id == located.id
        ).with_for_update().execution_options(populate_existing=True))
        if saved.consumed_at or saved.expires_at <= accounts.now():
            raise collaboration.denied()
        saved.consumed_at = accounts.now()
        grant = CollaborationGrant(ticket_id=saved.id, session_id=session.id,
                                    tactic_id=saved.tactic_id,
                                    node_instance_id=body.node_instance_id,
                                    connection_id=body.connection_id)
        db.add(grant)
        await db.flush()
        palette = ["#cb5a42", "#4b8b82", "#b58324", "#586b9b", "#8a4b78"]
        result = {"grant_id": str(grant.id), "tactic_id": str(saved.tactic_id),
                  "session_id": str(session.id),
                  "role": "viewer" if tactic._access_role == "viewer" else "editor",
                  "actor": {"id": str(user.id), "name": user.display_name,
                            "color": palette[user.id.int % len(palette)]},
                  "expires_at": session.expires_at.isoformat()}
    return response(result, status=201)


@internal_router.post("/grants/check")
async def check_grants(body: CheckBody):
    results = []
    for identity in body.grant_ids:
        try:
            async with collaboration.authorized_grant(identity) as (_, _, _, tactic, _):
                active = True
                role = "viewer" if tactic._access_role == "viewer" else "editor"
        except AppError as exc:
            if exc.status_code not in {401, 403, 404}:
                raise
            active = False
        results.append({"id": str(identity), "active": active,
                        "role": role if active else None})
    return response({"grants": results})


@internal_router.post("/grants/{grant_id}/release")
async def release(grant_id: uuid.UUID, body: EmptyBody):
    async with accounts.get_session_factory()() as db, db.begin():
        await db.execute(sa.update(CollaborationGrant).where(
            CollaborationGrant.id == grant_id, CollaborationGrant.closed_at.is_(None)
        ).values(closed_at=accounts.now()))
    return response({"ok": True})


@internal_router.post("/{tactic_id}/load")
async def load(tactic_id: uuid.UUID, body: GrantBody):
    async with collaboration.authorized_grant(body.grant_id, tactic_id) as (db, _, _, _, doc):
        result = await collaboration.load_view(db, doc)
    return response(result)


@internal_router.post("/{tactic_id}/operation-lookup")
async def lookup(tactic_id: uuid.UUID, body: LookupBody):
    async with collaboration.authorized_grant(body.grant_id, tactic_id) as (db, _, _, _, doc):
        if doc.epoch != body.epoch:
            raise AppError(409, "COLLAB_STALE_EPOCH", "文档代际已变化。")
        record = await db.get(collaboration.TacticOperation,
                              (tactic_id, body.epoch, body.operation_id))
        result = collaboration.operation_view(record) if record else None
    return response({"operation": result})


@internal_router.post("/{tactic_id}/commit")
async def commit(tactic_id: uuid.UUID, body: CommitBody, request: Request):
    return response(await collaboration.commit(body, tactic_id,
                                               audit_correlation=audit.request_id(request)))


@internal_router.post("/{tactic_id}/recoveries/{recovery_id}/read")
async def read_recovery(tactic_id: uuid.UUID, recovery_id: uuid.UUID, body: GrantBody):
    async with collaboration.authorized_grant(body.grant_id, tactic_id) as (db, _, _, tactic, _):
        access.require(tactic._access_role, "edit")
        item = await db.get(TacticRecoveryItem, recovery_id)
        if not item or item.tactic_id != tactic_id or item.expires_at <= accounts.now():
            raise documents.missing()
        result = {"id": str(item.id), "kind": item.kind, "snapshot": item.snapshot,
                  "texts": item.texts, "expires_at": item.expires_at.isoformat()}
    return response(result)
