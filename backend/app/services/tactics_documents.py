"""Trusted Node seed boundary and personal document directory transactions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import httpx
import sqlalchemy as sa

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.tactics_models import ApiIdempotency, Tactic, TacticDocument
from app.services import tactics_permissions as access
from app.services.tactics_accounts import now


def missing():
    return AppError(404, "TACTIC_NOT_FOUND", "资源不存在。")


def bad_input():
    return AppError(422, "VALIDATION_ERROR", "请求参数不合法。")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("utf-8")


def request_digest(value):
    return hashlib.sha256(json_bytes(value)).digest()


def idempotency_key(request):
    try:
        raw = request.headers.get("idempotency-key", "")
        key = uuid.UUID(raw)
        if str(key) != raw.lower():
            raise ValueError
        return key
    except ValueError:
        raise bad_input() from None


async def own_notebook(db, user_id, notebook_id, *, capability="read"):
    return (await access.notebooks_access(db, user_id, [notebook_id],
                                          capability=capability))[notebook_id]


async def owned_tactic(db, user_id, tactic_id, *, archived=False, capability="read"):
    notebook_id = await db.scalar(sa.select(Tactic.notebook_id).where(Tactic.id == tactic_id))
    if notebook_id is None:
        raise missing()
    notebook = await own_notebook(db, user_id, notebook_id,
                                  capability="manage" if archived else capability)
    tactic = await db.scalar(sa.select(Tactic).where(Tactic.id == tactic_id).with_for_update())
    document = await db.scalar(sa.select(TacticDocument).where(
        TacticDocument.tactic_id == tactic_id
    ).with_for_update())
    if document is None or (not archived and tactic.status != "active") or (
        tactic.status == "archived" and tactic.archived_at <= now() - timedelta(days=30)
    ):
        raise missing()
    tactic._access_role = notebook._access_role
    return tactic, document


def summary(tactic, document):
    return {"id": str(tactic.id), "notebook_id": str(tactic.notebook_id), "title": tactic.title,
            "side": tactic.side, "map": tactic.map, "revision": document.revision,
            "updated_at": tactic.updated_at.isoformat(),
            "archived_at": tactic.archived_at.isoformat() if tactic.archived_at else None,
            "source": tactic.source_attribution,
            "permissions": access.permissions(getattr(tactic, "_access_role", "owner"),
                                               active=tactic.status == "active")}


def view(tactic, document):
    return {"tactic": summary(tactic, document), "epoch": str(document.epoch),
            "projection": document.projection,
            "permissions": summary(tactic, document)["permissions"]}


async def lookup_idempotency(db, actor_id, scope, key, body):
    prior = await db.scalar(sa.select(ApiIdempotency).where(
        ApiIdempotency.actor_id == actor_id, ApiIdempotency.scope == scope,
        ApiIdempotency.key == key,
    ))
    if prior and prior.expires_at <= now():
        await db.delete(prior)
        await db.flush()
        return None
    if prior and not hmac.compare_digest(prior.request_digest, request_digest(body)):
        raise AppError(409, "OPERATION_ID_REUSED", "请求标识已用于其他内容。")
    return prior


def remember_idempotency(db, actor_id, scope, key, body, resource_id, result=None):
    db.add(ApiIdempotency(actor_id=actor_id, scope=scope, key=key,
                         request_digest=request_digest(body), resource_id=resource_id,
                         result=result, expires_at=now() + timedelta(hours=24)))


async def check_quota(db, notebook_id):
    count = await db.scalar(sa.select(sa.func.count()).select_from(Tactic).where(
        Tactic.notebook_id == notebook_id, Tactic.status == "active"
    ))
    if count >= 100:
        raise AppError(409, "TACTIC_LIMIT_REACHED", "个人本最多保留100条未删除战术。")


def validate_seed_packet(packet, document_id, title, side):
    """The Node service is trusted; validate its exact seed projection and transport digests."""
    if set(packet) != {"state_base64", "projection", "projection_json", "state_sha256",
                       "projection_sha256"}:
        raise ValueError("seed fields")
    raw = base64.b64decode(packet["state_base64"], validate=True)
    encoded_projection = packet["projection_json"].encode("utf-8")
    if not 0 < len(raw) <= 4194304 or len(encoded_projection) > 2097152:
        raise ValueError("seed size")
    if hashlib.sha256(raw).hexdigest() != packet["state_sha256"] or (
        hashlib.sha256(encoded_projection).hexdigest() != packet["projection_sha256"]
    ) or json_bytes(json.loads(encoded_projection)) != json_bytes(packet["projection"]):
        raise ValueError("seed digest")
    projection = packet["projection"]
    if set(projection) != {"structure", "fields"}:
        raise ValueError("seed projection")
    structure = projection["structure"]
    if set(structure) != {"schema_kind", "schema_version", "document_id", "map", "side",
                          "roles", "stages"} or len(structure["roles"]) != 1 or (
        len(structure["stages"]) != 1
    ):
        raise ValueError("seed structure")
    role = structure["roles"][0]
    stage = structure["stages"][0]
    role_id = str(uuid.UUID(role["id"], version=4))
    stage_id = str(uuid.UUID(stage["id"], version=4))
    if role_id != role["id"] or stage_id != stage["id"] or role_id == stage_id:
        raise ValueError("seed identity")
    expected_structure = {
        "schema_kind": "stage-board", "schema_version": 2, "document_id": str(document_id),
        "map": "mirage", "side": side,
        "roles": [{"id": role_id, "number": 1, "color": "#cb5a42"}],
        "stages": [{"id": stage_id, "version": 1, "initial_positions": {
            role_id: {"x": 50, "y": 50, "version": 1}}, "actions": [], "lineup_ids": []}],
    }
    expected_fields = {"tactic.title": title, "tactic.purpose": "", "tactic.applicability": "",
                       f"role.{role_id}.name": "角色 1", f"stage.{stage_id}.name": "阶段 1",
                       f"stage.{stage_id}.description": "", f"stage.{stage_id}.condition": ""}
    if json_bytes(structure) != json_bytes(expected_structure) or (
        projection["fields"] != expected_fields
    ):
        raise ValueError("seed content")
    return raw, projection, bytes.fromhex(packet["state_sha256"]), bytes.fromhex(
        packet["projection_sha256"]
    )


async def request_seed(document_id, title, side):
    settings = get_settings()
    key = settings.collab_internal_key.get_secret_value()
    if len(key.encode("utf-8")) < 32:
        raise AppError(503, "COLLAB_UNAVAILABLE", "协作服务暂不可用，请稍后重试。")
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            async with client.stream(
                "POST", settings.collab_internal_url.rstrip("/") + "/internal/seed-stage-document",
                headers={"internal-key": key},
                json={"document_id": str(document_id), "title": title, "side": side},
            ) as result:
                result.raise_for_status()
                chunks = bytearray()
                async for chunk in result.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 8 * 1024 * 1024:
                        raise ValueError("oversized seed")
        return validate_seed_packet(json.loads(chunks), document_id, title, side)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise AppError(503, "COLLAB_UNAVAILABLE", "协作服务暂不可用，请稍后重试。") from None


async def request_copy(document_id, projection):
    from app.schemas.tactics_document import validate_projection
    settings = get_settings()
    key = settings.collab_internal_key.get_secret_value()
    try:
        if len(key.encode()) < 32:
            raise ValueError("key unavailable")
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            async with client.stream(
                "POST", settings.collab_internal_url.rstrip("/") + "/internal/copy-stage-document",
                headers={"internal-key": key},
                json={"document_id": str(document_id), "projection": projection},
            ) as result:
                result.raise_for_status()
                chunks = bytearray()
                async for chunk in result.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 12 * 1024 * 1024:
                        raise ValueError("oversized copy")
        packet = json.loads(chunks)
        if set(packet) != {"state_base64", "projection", "projection_json", "state_sha256",
                           "projection_sha256"}:
            raise ValueError("copy fields")
        raw = base64.b64decode(packet["state_base64"], validate=True)
        encoded = packet["projection_json"].encode()
        if not 0 < len(raw) <= 4194304 or len(encoded) > 2097152 or (
            hashlib.sha256(raw).hexdigest() != packet["state_sha256"] or
            hashlib.sha256(encoded).hexdigest() != packet["projection_sha256"] or
            json_bytes(json.loads(encoded)) != json_bytes(packet["projection"])
        ):
            raise ValueError("copy digest")
        validate_projection(packet["projection"], str(document_id))
        return raw, packet["projection"], bytes.fromhex(packet["state_sha256"]), bytes.fromhex(
            packet["projection_sha256"])
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise AppError(503, "COLLAB_UNAVAILABLE", "协作服务暂不可用，请稍后重试。") from None
