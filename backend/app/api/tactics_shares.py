"""Owner-managed publication and anonymous, same-origin immutable snapshot reading."""

import secrets
import uuid
from datetime import timedelta
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from app.api import tactics_auth
from app.api.tactics_auth import AccountRoute, response
from app.core.errors import AppError
from app.core.routing import JsonBodyRoute
from app.db.tactics_models import (
    PersonalNotebook,
    ShareLink,
    ShareReadGrant,
    ShareVersion,
    ShareVersionItem,
    Tactic,
    TacticDocument,
    TacticVersion,
)
from app.schemas.tactics_auth import EmptyBody
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit
from app.services import tactics_documents as documents
from app.services import tactics_history as history
from app.services import tactics_permissions as access
from app.services import tactics_shares as shares
from app.services import tactics_teams as teams
from app.services.tactics_references import referenced_ids, resolve_reference


class ShareReadRoute(JsonBodyRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def checked(request):
            settings = tactics_auth.get_settings()
            if not settings.tactics_enabled:
                raise AppError(404, "ENDPOINT_NOT_FOUND", "接口不存在。")
            if request.headers.get("origin") != settings.public_base_url.rstrip("/"):
                raise AppError(403, "ORIGIN_INVALID", "不允许此来源的请求。")
            if request.query_params:
                raise documents.bad_input()
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 8192:
                    raise AppError(413, "REQUEST_TOO_LARGE", "请求内容过大。")
            request._body = bytes(chunks)
            return await original(request)

        return checked


router = APIRouter(prefix="/api/v1", route_class=AccountRoute, tags=["tactics-shares"])
public_router = APIRouter(
    prefix="/api/v1/shares", route_class=ShareReadRoute, tags=["shared-snapshots"]
)
Limit = Annotated[int, Query(ge=1, le=50)]
Cursor = Annotated[str | None, Query(max_length=2048)]
# Syntactically invalid string tokens are unavailable too; only JSON type/size errors are 422.
Secret = Annotated[str, StringConstraints(strict=True, max_length=128)]


class Revision(EmptyBody):
    id: uuid.UUID
    revision: Annotated[StrictInt, Field(ge=0, le=9007199254740991)]


class PublishBody(EmptyBody):
    allow_copy: StrictBool
    expected_revisions: Annotated[list[Revision], Field(max_length=100)]

    @model_validator(mode="after")
    def unique_sources(self):
        if len({item.id for item in self.expected_revisions}) != len(self.expected_revisions):
            raise ValueError("duplicate source IDs")
        return self


class CreateBody(PublishBody):
    notebook_id: uuid.UUID
    tactic_id: uuid.UUID | None
    allow_copy: StrictBool = False


class TokenBody(EmptyBody):
    token: Secret


class ReadBody(EmptyBody):
    read_grant: Secret


class ItemsBody(ReadBody):
    version_id: uuid.UUID
    cursor: Annotated[str | None, Field(max_length=2048)] = None


class LineupBody(ReadBody):
    version_id: uuid.UUID
    item_id: uuid.UUID
    lineup_id: uuid.UUID


class CopyBody(ReadBody):
    expected_version_id: uuid.UUID


class RestoreBody(EmptyBody):
    target_notebook_id: uuid.UUID


async def owned_link(db, user_id, identity):
    link = await db.get(ShareLink, identity)
    if link is None:
        raise documents.missing()
    await documents.own_notebook(db, user_id, link.owner_notebook_id, capability="manage")
    await db.refresh(link)
    return link


@router.get("/notebooks/{notebook_id}/shares")
async def list_shares(
    notebook_id: uuid.UUID, request: Request, limit: Limit = 20, cursor: Cursor = None
):
    async with accounts.authenticated(request) as (db, user, _, _):
        await documents.own_notebook(db, user.id, notebook_id, capability="manage")
        binding = [str(user.id), str(notebook_id)]
        rows = (
            await db.scalars(
                teams.page_query(
                    sa.select(ShareLink).where(ShareLink.owner_notebook_id == notebook_id),
                    ShareLink,
                    cursor,
                    binding,
                    limit,
                )
            )
        ).all()
        result = {
            "items": [shares.summary(link) for link in rows[:limit]],
            "next_cursor": teams.next_cursor(rows, limit, binding),
        }
    return response(result)


@router.post("/shares", status_code=201)
async def create_share(body: CreateBody, request: Request):
    key = documents.idempotency_key(request)
    payload, scope = body.model_dump(mode="json"), "share:create"
    token = None
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        await documents.own_notebook(db, user.id, body.notebook_id, capability="publish")
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            link = await owned_link(db, user.id, prior.resource_id)
        else:
            if body.tactic_id:
                belongs = await db.scalar(
                    sa.select(Tactic.notebook_id).where(Tactic.id == body.tactic_id)
                )
                if belongs != body.notebook_id:
                    raise documents.missing()
            token = secrets.token_urlsafe(32)
            link = ShareLink(
                owner_notebook_id=body.notebook_id,
                tactic_id=body.tactic_id,
                token_digest=accounts.digest(token),
                published_by=user.id,
            )
            db.add(link)
            await db.flush()
            await shares.publish(db, link, user.id, body.expected_revisions, body.allow_copy)
            await audit.append(db, "share.create", user.id, request=request,
                               tactic_id=link.tactic_id, notebook_id=link.owner_notebook_id,
                               resource_id=link.id)
            documents.remember_idempotency(db, user.id, scope, key, payload, link.id)
        result = {**shares.summary(link), "token": token, "token_available": token is not None}
    return response(result, status=201 if token else 200)


@router.post("/shares/{share_id}/publish")
async def publish_share(share_id: uuid.UUID, body: PublishBody, request: Request):
    key = documents.idempotency_key(request)
    payload, scope = body.model_dump(mode="json"), f"share:{share_id}:publish"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        link = await owned_link(db, user.id, share_id)
        if link.revoked_at:
            raise shares.unavailable()
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            return response(prior.result)
        await shares.publish(db, link, user.id, body.expected_revisions, body.allow_copy)
        result = shares.summary(link)
        await audit.append(db, "share.publish", user.id, request=request,
                           tactic_id=link.tactic_id, notebook_id=link.owner_notebook_id,
                           resource_id=link.id)
        documents.remember_idempotency(db, user.id, scope, key, payload, link.id, result)
    return response(result)


@router.post("/shares/{share_id}/revoke")
async def revoke_share(share_id: uuid.UUID, body: EmptyBody, request: Request):
    key = documents.idempotency_key(request)
    scope = f"share:{share_id}:revoke"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        link = await owned_link(db, user.id, share_id)
        if not await documents.lookup_idempotency(db, user.id, scope, key, {}):
            if link.revoked_at is None:
                link.revoked_at = accounts.now()
                await audit.append(db, "share.revoke", user.id, request=request,
                                   tactic_id=link.tactic_id, notebook_id=link.owner_notebook_id,
                                   resource_id=link.id)
            link.updated_at = accounts.now()
            documents.remember_idempotency(db, user.id, scope, key, {}, link.id)
    return response({"ok": True})


@public_router.post("/exchange")
async def exchange(body: TokenBody):
    value = secrets.token_urlsafe(32)
    async with accounts.get_session_factory()() as db, db.begin():
        link = await shares.lock_link(db, token=body.token)
        db.add(
            ShareReadGrant(
                grant_digest=accounts.digest(value),
                share_link_id=link.id,
                expires_at=accounts.now() + timedelta(seconds=300),
            )
        )
    return response({"read_grant": value, "expires_in": 300})


@public_router.post("/read")
async def read_share(body: ReadBody):
    async with accounts.get_session_factory()() as db, db.begin():
        link = await shares.lock_link(db, grant=body.read_grant)
        version = await db.get(ShareVersion, link.current_version_id)
        result = {
            "id": str(link.id),
            "version_id": str(version.id),
            "allow_copy": version.allow_copy,
            "published_at": version.created_at.isoformat(),
            "items": version.manifest,
        }
    return response(result)


@public_router.post("/items")
async def read_items(body: ItemsBody):
    async with accounts.get_session_factory()() as db, db.begin():
        link = await shares.lock_link(db, grant=body.read_grant)
        if link.current_version_id != body.version_id:
            raise shares.changed()
        index = shares.cursor_index(body.cursor, link, body.version_id)
        item = await db.scalar(
            sa.select(ShareVersionItem).where(
                ShareVersionItem.share_version_id == body.version_id,
                ShareVersionItem.order_index == index,
            )
        )
        if item is None:
            raise documents.bad_input()
        total = await db.scalar(
            sa.select(sa.func.count())
            .select_from(ShareVersionItem)
            .where(ShareVersionItem.share_version_id == body.version_id)
        )
        public_projection = {
            **item.projection,
            "structure": {
                **item.projection["structure"],
                "document_id": str(item.item_id),
            },
        }
        result = {
            "version_id": str(body.version_id),
            "items": [{"item_id": str(item.item_id), "projection": public_projection}],
            "next_cursor": shares.item_cursor(link, body.version_id, index + 1)
            if index + 1 < total
            else None,
        }
    return response(result)


@public_router.post("/lineups")
async def shared_lineup(body: LineupBody):
    async with accounts.get_session_factory()() as db, db.begin():
        link = await shares.lock_link(db, grant=body.read_grant)
        if link.current_version_id != body.version_id:
            raise shares.changed()
        item = await db.scalar(
            sa.select(ShareVersionItem).where(
                ShareVersionItem.share_version_id == body.version_id,
                ShareVersionItem.item_id == body.item_id,
            )
        )
        if item is None or str(body.lineup_id) not in referenced_ids(item.projection):
            raise shares.unavailable()
        result = await resolve_reference(db, body.lineup_id)
    return response(result)


async def personal_target(db, user_id):
    notebook = await db.scalar(
        sa.select(PersonalNotebook).where(
            PersonalNotebook.kind == "personal", PersonalNotebook.user_id == user_id
        )
    )
    if notebook is None:
        raise documents.missing()
    return notebook


async def copy_authorized(db, user_id, body):
    target = await personal_target(db, user_id)
    link = await shares.lock_link(db, grant=body.read_grant, target_id=target.id)
    if link.current_version_id != body.expected_version_id:
        raise shares.changed()
    if not link.allow_copy:
        raise AppError(403, "SHARE_COPY_FORBIDDEN", "作者未开放复制。")
    target._access_role = "owner"
    return link, target


async def batch_quota(db, target_id, amount):
    count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Tactic)
        .where(Tactic.notebook_id == target_id, Tactic.status == "active")
    )
    if count + amount > 100:
        raise AppError(409, "TACTIC_LIMIT_REACHED", "个人本剩余空间不足，整次复制未保存。")


async def replay_copies(db, prior, target):
    result = []
    for identity in prior.result["ids"]:
        tactic = await db.get(Tactic, uuid.UUID(identity))
        doc = await db.get(TacticDocument, uuid.UUID(identity))
        if tactic is None or tactic.notebook_id != target.id or tactic.status != "active":
            raise documents.missing()
        tactic._access_role = "owner"
        result.append(documents.summary(tactic, doc))
    return {"tactics": result, "source_version_id": prior.result["source_version_id"]}


@router.post("/shares/copy", status_code=201)
async def copy_share(body: CopyBody, request: Request):
    scope = "share:copy"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        key = documents.idempotency_key(request)
        link, target = await copy_authorized(db, user.id, body)
        payload = {"share_id": str(link.id), "version_id": str(body.expected_version_id)}
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            return response(await replay_copies(db, prior, target), status=201)
        version = await db.get(ShareVersion, body.expected_version_id)
        await batch_quota(db, target.id, len(version.manifest))
    async with shares.copy_spool_context(
        accounts.get_session_factory(), body.expected_version_id, documents.get_settings().temp_root
    ) as copies:
        async with accounts.authenticated(request, write=True) as (db, user, _, _):
            link, target = await copy_authorized(db, user.id, body)
            prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
            if prior:
                return response(await replay_copies(db, prior, target), status=201)
            await batch_quota(db, target.id, len(copies))
            results = [item async for item in shares.insert_spooled(db, target, user.id, copies)]
            for item in results:
                await audit.append(db, "tactic.copy.share", user.id, request=request,
                                   tactic_id=item["id"], resource_id=link.id)
            documents.remember_idempotency(
                db,
                user.id,
                scope,
                key,
                payload,
                link.id,
                {
                    "ids": [item["id"] for item in results],
                    "source_version_id": str(body.expected_version_id),
                },
            )
    return response(
        {"tactics": results, "source_version_id": str(body.expected_version_id)}, status=201
    )


async def history_source(db, user_id, tactic_id, version_id):
    tactic, _ = await documents.owned_tactic(db, user_id, tactic_id, archived=True)
    version = await db.get(TacticVersion, version_id)
    if version is None or version.tactic_id != tactic_id or version.expires_at <= accounts.now():
        raise documents.missing()
    return tactic, version


@router.get("/tactics/{tactic_id}/versions")
async def list_versions(
    tactic_id: uuid.UUID, request: Request, limit: Limit = 20, cursor: Cursor = None
):
    async with accounts.authenticated(request) as (db, user, _, _):
        await documents.owned_tactic(db, user.id, tactic_id, archived=True)
        binding = [str(user.id), str(tactic_id)]
        rows = (
            await db.scalars(
                teams.page_query(
                    sa.select(TacticVersion).where(
                        TacticVersion.tactic_id == tactic_id,
                        TacticVersion.expires_at > accounts.now(),
                    ),
                    TacticVersion,
                    cursor,
                    binding,
                    limit,
                )
            )
        ).all()
        result = {
            "items": [history.version_view(version) for version in rows[:limit]],
            "next_cursor": teams.next_cursor(rows, limit, binding),
        }
    return response(result)


@router.get("/tactics/{tactic_id}/versions/{version_id}")
async def get_version(tactic_id: uuid.UUID, version_id: uuid.UUID, request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        _, version = await history_source(db, user.id, tactic_id, version_id)
        result = {"version": history.version_view(version), "projection": version.projection}
    return response(result)


async def restore_access(db, user_id, tactic_id, version_id, target_id):
    source_id = await db.scalar(sa.select(Tactic.notebook_id).where(Tactic.id == tactic_id))
    if source_id is None:
        raise documents.missing()
    notebooks = await access.notebooks_access(db, user_id, [source_id, target_id])
    access.require(notebooks[source_id]._access_role, "manage")
    access.require(notebooks[target_id]._access_role, "create")
    tactic, version = await history_source(db, user_id, tactic_id, version_id)
    return tactic, version, notebooks[target_id]


@router.post("/tactics/{tactic_id}/versions/{version_id}/restore-copy", status_code=201)
async def restore_version(
    tactic_id: uuid.UUID, version_id: uuid.UUID, body: RestoreBody, request: Request
):
    key = documents.idempotency_key(request)
    payload, scope = body.model_dump(mode="json"), f"version:{version_id}:copy"
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        source, version, target = await restore_access(
            db, user.id, tactic_id, version_id, body.target_notebook_id
        )
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            copied, doc = await documents.owned_tactic(db, user.id, prior.resource_id)
            return response(documents.summary(copied, doc), status=201)
        await documents.check_quota(db, target.id)
        projection = version.projection
        creator = await db.get(accounts.TacticsUser, source.created_by)
        attribution = {
            "tactic_id": str(source.id),
            "title": projection["fields"]["tactic.title"],
            "author_display_name": creator.display_name,
            "copied_at": accounts.now().isoformat(),
        }
    identity = uuid.uuid4()
    state, projection, digest, pdigest = await documents.request_copy(identity, projection)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        _, _, target = await restore_access(
            db, user.id, tactic_id, version_id, body.target_notebook_id
        )
        prior = await documents.lookup_idempotency(db, user.id, scope, key, payload)
        if prior:
            copied, doc = await documents.owned_tactic(db, user.id, prior.resource_id)
        else:
            await documents.check_quota(db, target.id)
            copied = Tactic(
                id=identity,
                notebook_id=target.id,
                created_by=user.id,
                title=projection["fields"]["tactic.title"],
                side=projection["structure"]["side"],
                source_attribution=attribution,
            )
            db.add(copied)
            await db.flush()
            doc = TacticDocument(
                tactic_id=identity,
                yjs_state=state,
                projection=projection,
                state_digest=digest,
                projection_digest=pdigest,
            )
            db.add(doc)
            await db.flush()
            await audit.append(db, "tactic.restore_copy", user.id, request=request,
                               tactic_id=copied.id, team_id=target.team_id, resource_id=version_id)
            documents.remember_idempotency(db, user.id, scope, key, payload, identity)
        copied._access_role = target._access_role
        result = documents.summary(copied, doc)
    return response(result, status=201)
