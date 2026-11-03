"""Personal tactic directory and saved projections; browser bodies cannot overwrite documents."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request

from app.api.tactics_auth import AccountRoute, response
from app.core.errors import AppError
from app.db.tactics_models import (
    CollaborationGrant,
    CollaborationTicket,
    Tactic,
    TacticDocument,
)
from app.schemas.tactics import ArchiveTacticBody, CreateTacticBody
from app.schemas.tactics_auth import EmptyBody
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit
from app.services import tactics_documents as documents
from app.services import tactics_history as history
from app.services import tactics_permissions as access
from app.services import tactics_shares as shares
from app.services.tactics_references import referenced_ids, resolve_reference

router = APIRouter(prefix="/api/v1", route_class=AccountRoute, tags=["tactics"])


@router.get("/notebooks/{notebook_id}/tactics")
async def list_tactics(
    notebook_id: uuid.UUID, request: Request,
    deleted: Literal["false", "true"] = "false",
    q: Annotated[str, Query(max_length=100)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    async with accounts.authenticated(request) as (db, user, _, _):
        notebook = await documents.own_notebook(
            db, user.id, notebook_id, capability="manage" if deleted == "true" else "read"
        )
        query = sa.select(Tactic, TacticDocument).join(
            TacticDocument, TacticDocument.tactic_id == Tactic.id
        ).where(Tactic.notebook_id == notebook_id,
                Tactic.status == ("archived" if deleted == "true" else "active"))
        if deleted == "true":
            query = query.where(Tactic.archived_at > accounts.now() - timedelta(days=30))
        if q:
            query = query.where(sa.func.lower(Tactic.title).contains(q.lower(), autoescape=True))
        if cursor:
            try:
                boundary = json.loads(base64.b64decode(
                    cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
                ))
                if set(boundary) != {"n", "d", "q", "t", "i"} or (
                    boundary["n"] != str(notebook_id) or boundary["d"] != deleted
                    or boundary["q"] != q
                ):
                    raise ValueError
                at = datetime.fromisoformat(boundary["t"])
                last_id = uuid.UUID(boundary["i"])
                if at.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise documents.bad_input() from None
            query = query.where(sa.or_(Tactic.updated_at < at, sa.and_(
                Tactic.updated_at == at, Tactic.id < last_id
            )))
        rows = (await db.execute(query.order_by(
            Tactic.updated_at.desc(), Tactic.id.desc()
        ).limit(limit + 1))).all()
        for tactic, _ in rows[:limit]:
            tactic._access_role = notebook._access_role
        items = [documents.summary(tactic, doc) for tactic, doc in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            tactic = rows[limit - 1][0]
            next_cursor = base64.urlsafe_b64encode(documents.json_bytes({
                "n": str(notebook_id), "d": deleted, "q": q,
                "t": tactic.updated_at.isoformat(), "i": str(tactic.id),
            })).decode().rstrip("=")
    return response({"items": items, "next_cursor": next_cursor,
                     "permissions": access.permissions(notebook._access_role, notebook=True)})


@router.post("/notebooks/{notebook_id}/tactics", status_code=201)
async def create_tactic(notebook_id: uuid.UUID, body: CreateTacticBody, request: Request):
    key = documents.idempotency_key(request)
    payload = body.model_dump()
    scope = f"notebook:{notebook_id}:create"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        await documents.own_notebook(db, user.id, notebook_id, capability="create")
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            tactic, doc = await documents.owned_tactic(
                db, user.id, prior.resource_id
            )
            return response(documents.view(tactic, doc), status=201)
        await documents.check_quota(db, notebook_id)
    tactic_id = uuid.uuid4()
    # Seed/network work is deliberately outside all PostgreSQL locks.
    state, projection, state_digest, projection_digest = await documents.request_seed(
        tactic_id, body.title, body.side
    )
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        notebook = await documents.own_notebook(db, user.id, notebook_id, capability="create")
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            tactic, doc = await documents.owned_tactic(
                db, user.id, prior.resource_id
            )
        else:
            await documents.check_quota(db, notebook_id)
            tactic = Tactic(id=tactic_id, notebook_id=notebook_id, created_by=user.id,
                            title=body.title, side=body.side)
            db.add(tactic)
            await db.flush()
            doc = TacticDocument(tactic_id=tactic_id, yjs_state=state, projection=projection,
                                 state_digest=state_digest, projection_digest=projection_digest)
            db.add(doc)
            await audit.append(db, "tactic.create", user.id, request=request,
                               tactic_id=tactic.id, team_id=notebook.team_id,
                               resource_id=notebook_id)
            documents.remember_idempotency(db, user.id, scope, key, payload, tactic_id)
            await db.flush()
        tactic._access_role = notebook._access_role
        result = documents.view(tactic, doc)
    return response(result, status=201)


@router.get("/tactics/{tactic_id}")
async def get_tactic(tactic_id: uuid.UUID, request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        tactic, doc = await documents.owned_tactic(db, user.id, tactic_id)
        result = documents.view(tactic, doc)
    return response(result)


@router.get("/tactics/{tactic_id}/archived")
async def get_archived_tactic(tactic_id: uuid.UUID, request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        tactic, doc = await documents.owned_tactic(db, user.id, tactic_id, archived=True)
        if tactic.status != "archived":
            raise documents.missing()
        result = documents.view(tactic, doc)
    return response(result)


@router.get("/tactics/{tactic_id}/lineups/{lineup_id}")
async def tactic_lineup(tactic_id: uuid.UUID, lineup_id: uuid.UUID, request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        _, doc = await documents.owned_tactic(db, user.id, tactic_id)
        if str(lineup_id) not in referenced_ids(doc.projection):
            raise documents.missing()
        result = await resolve_reference(db, lineup_id)
    return response(result)


@router.get("/tactics/{tactic_id}/present")
async def present_tactic(tactic_id: uuid.UUID, request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        tactic, doc = await documents.owned_tactic(db, user.id, tactic_id)
        result = {"tactic": documents.summary(tactic, doc), "epoch": str(doc.epoch),
                  "revision": doc.revision, "projection": doc.projection}
    return response(result)


@router.post("/tactics/{tactic_id}/archive")
async def archive_tactic(tactic_id: uuid.UUID, body: ArchiveTacticBody, request: Request):
    key = documents.idempotency_key(request)
    payload, scope = body.model_dump(), f"tactic:{tactic_id}:archive"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        tactic, doc = await documents.owned_tactic(db, user.id, tactic_id, archived=True)
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            return response(prior.result)
        if tactic.status != "active":
            raise AppError(409, "TACTIC_ARCHIVED", "战术已在回收站中。")
        if doc.revision != body.expected_revision or doc.revision >= 9007199254740991:
            raise AppError(409, "TACTIC_CONFLICT", "版本已变化，请重新读取后重试。",
                           {"current_revision": doc.revision})
        at = accounts.now()
        await history.save_history(db, doc, user.id, "archive")
        await shares.revoke_sources(db, tactic.notebook_id, tactic.id)
        tactic.status, tactic.archived_at, tactic.updated_at = "archived", at, at
        doc.epoch, doc.revision, doc.updated_at = uuid.uuid4(), doc.revision + 1, at
        await db.execute(sa.update(CollaborationGrant).where(
            CollaborationGrant.tactic_id == tactic.id, CollaborationGrant.closed_at.is_(None)
        ).values(closed_at=at))
        await db.execute(sa.update(CollaborationTicket).where(
            CollaborationTicket.tactic_id == tactic.id,
            CollaborationTicket.consumed_at.is_(None)
        ).values(consumed_at=at))
        result = {"id": str(tactic.id), "archived_at": at.isoformat(), "revision": doc.revision}
        await audit.append(db, "tactic.archive", user.id, request=request,
                           tactic_id=tactic.id, resource_id=tactic.notebook_id)
        documents.remember_idempotency(db, user.id, scope, key, payload, tactic.id, result)
    return response(result)


@router.post("/tactics/{tactic_id}/restore")
async def restore_tactic(tactic_id: uuid.UUID, body: EmptyBody, request: Request):
    key = documents.idempotency_key(request)
    scope = f"tactic:{tactic_id}:restore"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        tactic, doc = await documents.owned_tactic(db, user.id, tactic_id, archived=True)
        prior = await documents.lookup_idempotency(db, user.id, scope, key, {})
        if prior:
            return response(prior.result)
        if tactic.status == "archived":
            await documents.check_quota(db, tactic.notebook_id)
            if doc.revision >= 9007199254740991:
                raise documents.bad_input()
            at = accounts.now()
            tactic.status, tactic.archived_at, tactic.updated_at = "active", None, at
            doc.epoch, doc.revision, doc.updated_at = uuid.uuid4(), doc.revision + 1, at
            await audit.append(db, "tactic.restore", user.id, request=request,
                               tactic_id=tactic.id, resource_id=tactic.notebook_id)
        result = documents.summary(tactic, doc)
        documents.remember_idempotency(db, user.id, scope, key, {}, tactic.id, result)
    return response(result)
