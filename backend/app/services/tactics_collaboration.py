"""Permission rechecks and atomic Node commits; no browser-selected identities are trusted."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import sqlalchemy as sa

from app.core.errors import AppError
from app.db.tactics_models import (
    CollaborationGrant,
    TacticOperation,
    TacticRecoveryItem,
    TacticsSession,
    TacticsUser,
)
from app.schemas.tactics_document import validate_projection
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit
from app.services import tactics_documents as documents
from app.services.tactics_references import check_new_references


def denied():
    return AppError(401, "USER_AUTH_REQUIRED", "协作身份已失效，请重新登录。")


async def lock_identity(db, session_id):
    owner_id = await db.scalar(sa.select(TacticsSession.user_id).where(
        TacticsSession.id == session_id
    ))
    user = await db.scalar(sa.select(TacticsUser).where(
        TacticsUser.id == owner_id
    ).with_for_update(key_share=True)) if owner_id else None
    session = await db.scalar(sa.select(TacticsSession).where(
        TacticsSession.id == session_id
    ).with_for_update()) if user else None
    if not session or session.revoked_at or session.expires_at <= accounts.now() or (
        session.credential_version != user.credential_version
    ):
        raise denied()
    return user, session


@asynccontextmanager
async def authorized_grant(grant_id, tactic_id=None):
    async with accounts.get_session_factory()() as db, db.begin():
        session_id = await db.scalar(sa.select(CollaborationGrant.session_id).where(
            CollaborationGrant.id == grant_id
        ))
        if not session_id:
            raise denied()
        user, _ = await lock_identity(db, session_id)
        # Locate without locking; all permission/document locks precede the grant lock.
        located = await db.get(CollaborationGrant, grant_id)
        tactic, document = await documents.owned_tactic(db, user.id, located.tactic_id)
        grant = await db.scalar(sa.select(CollaborationGrant).where(
            CollaborationGrant.id == grant_id
        ).with_for_update().execution_options(populate_existing=True))
        if grant.closed_at or (tactic_id is not None and grant.tactic_id != tactic_id):
            raise denied()
        yield db, user, grant, tactic, document


def operation_view(record):
    return {"operation_id": str(record.operation_id), "epoch": str(record.epoch),
            "actor_id": str(record.actor_id), "fingerprint": record.fingerprint.hex(),
            "type": record.type, "revision": record.revision}


async def recoveries(db, tactic_id):
    records = (await db.scalars(sa.select(TacticRecoveryItem).where(
        TacticRecoveryItem.tactic_id == tactic_id, TacticRecoveryItem.expires_at > accounts.now()
    ).order_by(TacticRecoveryItem.created_at.desc()))).all()
    return [{"id": str(item.id), "kind": item.kind, "entity_id": str(item.entity_id),
             "created_at": item.created_at.isoformat(), "expires_at": item.expires_at.isoformat()}
            for item in records]


async def load_view(db, document):
    return {"document_id": str(document.tactic_id), "epoch": str(document.epoch),
            "revision": document.revision,
            "state_base64": base64.b64encode(document.yjs_state).decode(),
            "projection": document.projection, "state_sha256": document.state_digest.hex(),
            "projection_sha256": document.projection_digest.hex(),
            "recoveries": await recoveries(db, document.tactic_id)}


def validate_commit_packet(body, tactic_id):
    state = base64.b64decode(body.state_base64, validate=True)
    projection_json = body.projection_json.encode("utf-8")
    payload_json = body.payload_json.encode("utf-8")
    if not 0 < len(state) <= 4194304 or len(projection_json) > 2097152:
        raise ValueError("document size")
    projection, payload = json.loads(projection_json), json.loads(payload_json)
    if documents.json_bytes(projection) != documents.json_bytes(body.projection) or (
        documents.json_bytes(payload) != documents.json_bytes(body.original_payload)
    ):
        raise ValueError("transport projection")
    if hashlib.sha256(state).hexdigest() != body.state_sha256 or (
        hashlib.sha256(projection_json).hexdigest() != body.projection_sha256
    ):
        raise ValueError("digest")
    fingerprint = hashlib.sha256(
        f"{body.type}\n{body.epoch}\n".encode() + payload_json
    ).digest()
    if not hmac.compare_digest(fingerprint.hex(), body.fingerprint):
        raise ValueError("operation fingerprint")
    validate_projection(projection, str(tactic_id))
    token = '"structure":'
    start = body.projection_json.rfind(token) + len(token)
    _, end = json.JSONDecoder().raw_decode(body.projection_json, start)
    if len(body.projection_json[start:end].encode("utf-8")) > 1048576:
        raise ValueError("structure size")
    if body.type == "text.update":
        if set(payload) != {"update"}:
            raise ValueError("text update")
        update = base64.b64decode(payload["update"], validate=True)
        if not 0 < len(update) <= 1048576:
            raise ValueError("text update size")
    elif set(payload) != {"expected_revision", "command"} or (
        payload["expected_revision"] != body.expected_revision
    ):
        raise ValueError("structure command")
    return state, projection, fingerprint


def preserve_removed(db, document, next_projection, actor_id):
    old = document.projection
    next_stages = {stage["id"]: stage for stage in next_projection["structure"]["stages"]}
    expires = accounts.now() + timedelta(days=30)
    for stage in old["structure"]["stages"]:
        active = next_stages.get(stage["id"])
        if active is None:
            keys = [f"stage.{stage['id']}.{name}" for name in ["name", "description", "condition"]]
            keys += [f"action.{action['id']}.description" for action in stage["actions"]]
            db.add(TacticRecoveryItem(
                tactic_id=document.tactic_id, source_epoch=document.epoch, entity_id=stage["id"],
                kind="stage", snapshot={"stage": stage},
                texts={key: old["fields"][key] for key in keys}, deleted_by=actor_id,
                expires_at=expires,
            ))
            continue
        active_ids = {action["id"] for action in active["actions"]}
        for action in stage["actions"]:
            if action["id"] not in active_ids:
                key = f"action.{action['id']}.description"
                db.add(TacticRecoveryItem(
                    tactic_id=document.tactic_id, source_epoch=document.epoch,
                    entity_id=action["id"], kind="action",
                    snapshot={"stage_id": stage["id"], "action": action},
                    texts={key: old["fields"][key]}, deleted_by=actor_id, expires_at=expires,
                ))


async def command_recovery(db, command, tactic_id, epoch, user_id):
    """Recheck durable undo/recovery ownership inside the already-authorized commit transaction."""
    undo = command.get("type") == "object.undo"
    if undo:
        source = await db.get(
            TacticOperation, (tactic_id, epoch, command.get("source_operation_id"))
        )
        if not source or source.actor_id != user_id or source.type != "stage.command":
            raise AppError(409, "TACTIC_CONFLICT", "撤销来源已失效。")
    if command.get("type") != "recovery.restore" and not (undo and command.get("recovery_id")):
        return None
    recovery = await db.get(TacticRecoveryItem, command.get("recovery_id"))
    if not recovery or recovery.tactic_id != tactic_id or (
        recovery.expires_at <= accounts.now()
    ) or (undo and (recovery.source_epoch != epoch or recovery.deleted_by != user_id)):
        raise AppError(409, "TACTIC_CONFLICT", "恢复来源已失效。")
    return recovery


async def commit(body, tactic_id, *, audit_correlation=None):
    try:
        state, projection, fingerprint = validate_commit_packet(body, tactic_id)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise documents.bad_input() from None
    async with authorized_grant(body.grant_id, tactic_id) as (db, user, _, tactic, document):
        from app.services.tactics_permissions import require
        require(tactic._access_role, "edit")
        if document.epoch != body.epoch:
            raise AppError(409, "COLLAB_STALE_EPOCH", "文档代际已变化，请保留草稿后重连。")
        prior = await db.get(TacticOperation, (tactic_id, body.epoch, body.operation_id))
        if prior:
            if prior.actor_id != user.id or prior.type != body.type or not hmac.compare_digest(
                prior.fingerprint, fingerprint
            ):
                raise AppError(409, "OPERATION_ID_REUSED", "操作标识已用于其他内容。")
            return {"operation_id": str(prior.operation_id), "epoch": str(prior.epoch),
                    "revision": prior.revision, "duplicate": True,
                    "state_sha256": document.state_digest.hex()}
        if document.revision != body.expected_revision or document.revision >= 9007199254740991:
            raise AppError(409, "TACTIC_CONFLICT", "版本已变化，请重同步后重试。",
                           {"current_revision": document.revision})
        if body.type == "text.update" and documents.json_bytes(
            projection["structure"]
        ) != documents.json_bytes(document.projection["structure"]):
            raise documents.bad_input()
        command = body.original_payload.get("command", {})
        recovery = await command_recovery(db, command, tactic_id, body.epoch, user.id)
        await check_new_references(db, document.projection, projection, recovery)
        preserve_removed(db, document, projection, user.id)
        document.yjs_state, document.projection = state, projection
        document.state_digest = bytes.fromhex(body.state_sha256)
        document.projection_digest = bytes.fromhex(body.projection_sha256)
        document.revision += 1
        document.updated_at = tactic.updated_at = accounts.now()
        tactic.title = projection["fields"]["tactic.title"]
        tactic.side = projection["structure"]["side"]
        db.add(TacticOperation(tactic_id=tactic_id, epoch=document.epoch,
                              operation_id=body.operation_id, actor_id=user.id, type=body.type,
                              fingerprint=fingerprint, revision=document.revision))
        action = "document.text.update" if body.type == "text.update" else (
            "document." + command.get("type", ""))
        await audit.append(db, action, user.id, tactic_id=tactic_id,
                           resource_id=body.operation_id, correlation=audit_correlation)
        result = {"operation_id": str(body.operation_id), "epoch": str(document.epoch),
                  "revision": document.revision, "duplicate": False,
                  "state_sha256": document.state_digest.hex()}
    return result
