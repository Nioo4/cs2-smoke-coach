"""Authenticated management endpoints for the first teaching workflow."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.routing import JsonBodyRoute
from app.db.models import (
    Lineup,
    LineupStatus,
    Origin,
    OriginAlias,
    Target,
    TargetAlias,
    TutorialVideo,
    VideoStatus,
    utc_now,
)
from app.db.session import get_session_factory
from app.schemas.admin import (
    AdminLineupOut,
    AdminSessionOut,
    LineupCreate,
    LineupPatch,
    MetricsSummary,
    PointCreate,
    PointOut,
    PointPatch,
    PublishRequest,
    TargetPositionPatch,
)
from app.schemas.public import MapAsset
from app.services.map_catalog import get_map_catalog
from app.services.metrics import add_admin_action, metrics_summary

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], route_class=JsonBodyRoute)
_AUTH_SLOTS = asyncio.Semaphore(2)
_PASSWORD_HASHER = PasswordHasher()
CONTENT_WRITE_LOCK_KEY = 1397575499
_POSITION_QUANTUM = Decimal("0.01")


def _auth_failure() -> AppError:
    return AppError(401, "ADMIN_AUTH_REQUIRED", "需要管理员认证。")


def _validation_error(*fields: dict[str, str]) -> AppError:
    return AppError(
        422,
        "VALIDATION_ERROR",
        "请求参数不合法。",
        {"fields": list(fields)},
    )


def _decode_basic(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, separator, encoded = header.partition(" ")
    if not separator or scheme.casefold() != "basic" or not encoded:
        return None
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
        decoded = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def _verify_password(encoded_hash: str, password: str) -> bool:
    try:
        raw_hash = base64.b64decode(encoded_hash, validate=True).decode("utf-8")
        return bool(_PASSWORD_HASHER.verify(raw_hash, password))
    except (binascii.Error, UnicodeDecodeError, InvalidHashError, VerificationError):
        return False


async def _await_task_settled(task: asyncio.Task[bool]) -> tuple[bool, bool]:
    """Wait for Argon2 work to finish before returning an auth slot."""

    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def require_admin(request: Request) -> str:
    credentials = _decode_basic(request.headers.get("authorization"))
    if credentials is None:
        raise _auth_failure()
    username, password = credentials
    settings = get_settings()
    username_ok = hmac.compare_digest(
        username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    if not username_ok or not settings.admin_password_hash_b64:
        raise _auth_failure()
    try:
        await asyncio.wait_for(_AUTH_SLOTS.acquire(), timeout=1.0)
    except TimeoutError:
        raise AppError(429, "ADMIN_AUTH_BUSY", "管理员认证繁忙，请稍后重试。") from None
    try:
        verify_task = asyncio.create_task(
            asyncio.to_thread(_verify_password, settings.admin_password_hash_b64, password)
        )
        verified, interrupted = await _await_task_settled(verify_task)
    finally:
        _AUTH_SLOTS.release()
    if interrupted:
        raise asyncio.CancelledError
    if not verified:
        raise _auth_failure()
    return username


async def _session() -> AsyncSession:
    async with get_session_factory()() as session:
        yield session


_ADMIN_DEPENDENCY = Depends(require_admin)
_SESSION_DEPENDENCY = Depends(_session)


async def _content_lock(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '5s'"))
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": CONTENT_WRITE_LOCK_KEY}
        )
    except OperationalError as exc:
        if "lock timeout" in str(exc).casefold():
            raise AppError(409, "CONTENT_WRITE_BUSY", "内容写入繁忙，请稍后重试。") from None
        raise


def _integrity_error(exc: IntegrityError, resource: str) -> AppError:
    constraint = str(
        getattr(exc.orig, "diag", None) and getattr(exc.orig.diag, "constraint_name", "")
    )
    if resource == "target" and constraint in {"targets_slug_key", "uq_targets_slug"}:
        return AppError(409, "TARGET_SLUG_CONFLICT", "Target slug 已存在。")
    if resource == "target" and constraint == "uq_target_alias_normalized":
        return AppError(409, "TARGET_ALIAS_CONFLICT", "Target alias 已存在。")
    if resource == "origin" and constraint in {"origins_slug_key", "uq_origins_slug"}:
        return AppError(409, "ORIGIN_SLUG_CONFLICT", "Origin slug 已存在。")
    if resource == "origin" and constraint == "uq_origin_alias_normalized":
        return AppError(409, "ORIGIN_ALIAS_CONFLICT", "Origin alias 已存在。")
    return AppError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")


def _point_dict(point: Target | Origin, aliases: list[str]) -> dict[str, object]:
    return {
        "id": str(point.id),
        "slug": point.slug,
        "name_zh": point.name_zh,
        "name_en": point.name_en,
        "description": point.description,
        "map_x": None if point.map_x is None else float(point.map_x),
        "map_y": None if point.map_y is None else float(point.map_y),
        "sort_order": point.sort_order,
        "aliases": aliases,
        "is_active": point.is_active,
        "created_at": point.created_at,
        "updated_at": point.updated_at,
    }


async def _aliases(session: AsyncSession, model, foreign_key, item_id: UUID) -> list[str]:
    rows = (
        (
            await session.execute(
                select(model.alias)
                .where(foreign_key == item_id)
                .order_by(model.normalized_alias, model.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _point_list(
    model, alias_model, include_archived: bool, session: AsyncSession
) -> list[dict[str, object]]:
    statement = select(model)
    if not include_archived:
        statement = statement.where(model.is_active.is_(True))
    statement = statement.order_by(
        model.is_active.desc(), model.sort_order, model.name_zh, model.id
    )
    points = (await session.execute(statement)).scalars().all()
    result: list[dict[str, object]] = []
    for point in points:
        result.append(
            _point_dict(
                point,
                await _aliases(
                    session,
                    alias_model,
                    alias_model.target_id if model is Target else alias_model.origin_id,
                    point.id,
                ),
            )
        )
    return result


@router.get("/session", response_model=AdminSessionOut)
async def admin_session(_username: str = _ADMIN_DEPENDENCY) -> dict[str, bool]:
    return {"authenticated": True}


@router.get("/maps/mirage", response_model=MapAsset)
async def admin_map_catalog(_username: str = _ADMIN_DEPENDENCY) -> dict[str, str]:
    map_data = get_map_catalog()
    return {
        "slug": map_data.slug,
        "name_zh": map_data.name_zh,
        "asset_url": map_data.asset_url,
        "asset_status": map_data.asset_status,
    }


@router.get("/metrics/summary", response_model=MetricsSummary)
async def admin_metrics_summary(
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await metrics_summary(session)


@router.get("/targets", response_model=list[PointOut])
async def list_targets(
    include_archived: bool = Query(False),
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> list[dict[str, object]]:
    return await _point_list(Target, TargetAlias, include_archived, session)


@router.get("/origins", response_model=list[PointOut])
async def list_origins(
    include_archived: bool = Query(False),
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> list[dict[str, object]]:
    return await _point_list(Origin, OriginAlias, include_archived, session)


async def _create_point(
    body: PointCreate,
    model,
    alias_model,
    session: AsyncSession,
    request_id: str,
) -> dict[str, object]:
    async with session.begin():
        await _content_lock(session)
        limit = 100
        if await session.scalar(select(func.count()).select_from(model)) >= limit:
            raise AppError(409, "CATALOG_LIMIT_REACHED", "目录已达到当前阶段上限。")
        point = model(
            slug=body.slug,
            name_zh=body.name_zh,
            name_en=body.name_en,
            description=body.description,
            sort_order=body.sort_order,
            is_active=True,
        )
        session.add(point)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise _integrity_error(exc, "target" if model is Target else "origin") from None
        for alias in body.aliases:
            foreign_key = "target_id" if model is Target else "origin_id"
            session.add(
                alias_model(
                    **{
                        foreign_key: point.id,
                        "alias": alias,
                        "normalized_alias": _normalize_alias(alias),
                    }
                )
            )
        try:
            await session.flush()
        except IntegrityError as exc:
            raise _integrity_error(exc, "target" if model is Target else "origin") from None
        add_admin_action(session, request_id)
        output = _point_dict(point, body.aliases)
    return output


def _normalize_alias(value: str) -> str:
    from app.services.normalization import normalize_text

    return normalize_text(value)


@router.post("/targets", response_model=PointOut, status_code=201)
async def create_target(
    body: PointCreate,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _create_point(body, Target, TargetAlias, session, request.state.request_id)


@router.post("/origins", response_model=PointOut, status_code=201)
async def create_origin(
    body: PointCreate,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _create_point(body, Origin, OriginAlias, session, request.state.request_id)


async def _point_for_update(point_id: UUID, model, session: AsyncSession) -> object:
    point = await session.get(model, point_id, with_for_update=True)
    if point is None:
        resource = "Target" if model is Target else "Origin"
        raise AppError(404, f"ADMIN_{resource.upper()}_NOT_FOUND", f"{resource} 不存在。")
    return point


async def _point_patch(
    point_id: UUID,
    body: PointPatch,
    model,
    alias_model,
    session: AsyncSession,
    request_id: str,
) -> dict[str, object]:
    fields = body.model_fields_set
    for field in ("name_zh", "description", "aliases"):
        if field in fields and getattr(body, field) is None:
            raise _validation_error({"path": field, "reason": "invalid_value"})
    async with session.begin():
        await _content_lock(session)
        point = await _point_for_update(point_id, model, session)
        changed = False
        for field in ("name_zh", "name_en", "description", "sort_order"):
            if field in fields:
                value = getattr(body, field)
                if getattr(point, field) != value:
                    setattr(point, field, value)
                    changed = True
        if "aliases" in fields:
            foreign_key = alias_model.target_id if model is Target else alias_model.origin_id
            current_aliases = await _aliases(session, alias_model, foreign_key, point.id)
            if set(current_aliases) != set(body.aliases or []):
                await session.execute(sa.delete(alias_model).where(foreign_key == point.id))
                for alias in body.aliases or []:
                    session.add(
                        alias_model(
                            **{
                                "target_id" if model is Target else "origin_id": point.id,
                                "alias": alias,
                                "normalized_alias": _normalize_alias(alias),
                            }
                        )
                    )
                changed = True
        if changed:
            point.updated_at = utc_now()
            add_admin_action(session, request_id)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise _integrity_error(exc, "target" if model is Target else "origin") from None
        aliases = await _aliases(
            session,
            alias_model,
            alias_model.target_id if model is Target else alias_model.origin_id,
            point.id,
        )
        output = _point_dict(point, aliases)
    return output


def _rounded_position(value: float | None) -> Decimal | None:
    return (
        None
        if value is None
        else Decimal(str(value)).quantize(_POSITION_QUANTUM, rounding=ROUND_HALF_UP)
    )


@router.patch("/targets/{target_id}/position", response_model=PointOut)
async def patch_target_position(
    target_id: UUID,
    body: TargetPositionPatch,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    requested_x = _rounded_position(body.map_x)
    requested_y = _rounded_position(body.map_y)
    async with session.begin():
        await _content_lock(session)
        target = await _point_for_update(target_id, Target, session)
        if not target.is_active:
            raise AppError(409, "POINT_INACTIVE", "归档 Target 不可定位。")
        if requested_x is None and get_map_catalog().asset_status == "approved":
            published = await session.scalar(
                select(func.count())
                .select_from(Lineup)
                .where(
                    Lineup.target_id == target.id,
                    Lineup.status == LineupStatus.published,
                )
            )
            if published:
                raise AppError(
                    409,
                    "PUBLISHED_TARGET_POSITION_REQUIRED",
                    "已有公开路线的 Target 必须保留地图位置。",
                )
        changed = target.map_x != requested_x or target.map_y != requested_y
        if changed:
            target.map_x = requested_x
            target.map_y = requested_y
            target.updated_at = utc_now()
            add_admin_action(session, request.state.request_id)
        await session.flush()
        aliases = await _aliases(session, TargetAlias, TargetAlias.target_id, target.id)
        output = _point_dict(target, aliases)
    return output


async def _point_archive_or_restore(
    point_id: UUID,
    model,
    alias_model,
    *,
    archive: bool,
    session: AsyncSession,
    request_id: str,
) -> dict[str, object]:
    async with session.begin():
        await _content_lock(session)
        point = await _point_for_update(point_id, model, session)
        desired_active = not archive
        if point.is_active == desired_active:
            aliases = await _aliases(
                session,
                alias_model,
                alias_model.target_id if model is Target else alias_model.origin_id,
                point.id,
            )
            return _point_dict(point, aliases)
        if archive:
            reference_column = Lineup.target_id if model is Target else Lineup.origin_id
            active_references = await session.scalar(
                select(func.count())
                .select_from(Lineup)
                .where(reference_column == point.id, Lineup.status != LineupStatus.archived)
            )
            if active_references:
                code = "TARGET_IN_USE" if model is Target else "ORIGIN_IN_USE"
                label = "Target" if model is Target else "Origin"
                raise AppError(409, code, f"仍有路线引用该{label}，无法归档。")
        point.is_active = desired_active
        point.updated_at = utc_now()
        add_admin_action(session, request_id)
        await session.flush()
        aliases = await _aliases(
            session,
            alias_model,
            alias_model.target_id if model is Target else alias_model.origin_id,
            point.id,
        )
        return _point_dict(point, aliases)


@router.patch("/targets/{target_id}", response_model=PointOut)
async def patch_target(
    target_id: UUID,
    body: PointPatch,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _point_patch(
        target_id, body, Target, TargetAlias, session, request.state.request_id
    )


@router.patch("/origins/{origin_id}", response_model=PointOut)
async def patch_origin(
    origin_id: UUID,
    body: PointPatch,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _point_patch(
        origin_id, body, Origin, OriginAlias, session, request.state.request_id
    )


@router.post("/targets/{target_id}/archive", response_model=PointOut)
async def archive_target(
    target_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _point_archive_or_restore(
        target_id,
        Target,
        TargetAlias,
        archive=True,
        session=session,
        request_id=request.state.request_id,
    )


@router.post("/targets/{target_id}/restore", response_model=PointOut)
async def restore_target(
    target_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _point_archive_or_restore(
        target_id,
        Target,
        TargetAlias,
        archive=False,
        session=session,
        request_id=request.state.request_id,
    )


@router.post("/origins/{origin_id}/archive", response_model=PointOut)
async def archive_origin(
    origin_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _point_archive_or_restore(
        origin_id,
        Origin,
        OriginAlias,
        archive=True,
        session=session,
        request_id=request.state.request_id,
    )


@router.post("/origins/{origin_id}/restore", response_model=PointOut)
async def restore_origin(
    origin_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _point_archive_or_restore(
        origin_id,
        Origin,
        OriginAlias,
        archive=False,
        session=session,
        request_id=request.state.request_id,
    )


def _lineup_status_order() -> sa.Case:
    return sa.case(
        (Lineup.status == LineupStatus.published, 0),
        (Lineup.status == LineupStatus.needs_revalidation, 1),
        (Lineup.status == LineupStatus.draft, 2),
        else_=3,
    )


async def _lineup_row(session: AsyncSession, lineup_id: UUID):
    statement = (
        select(Lineup, Target, Origin, TutorialVideo)
        .join(Target, Target.id == Lineup.target_id)
        .join(Origin, Origin.id == Lineup.origin_id)
        .outerjoin(TutorialVideo, TutorialVideo.lineup_id == Lineup.id)
        .where(Lineup.id == lineup_id)
    )
    return (await session.execute(statement)).first()


def _media_url(video: TutorialVideo, *, poster: bool = False) -> str | None:
    if (
        video.status != VideoStatus.ready
        or not video.relative_path
        or not video.poster_relative_path
    ):
        return None
    path = video.poster_relative_path if poster else video.relative_path
    return f"/media/{path}?v={(video.checksum_sha256 or '')[:12]}"


def _admin_lineup_output(row) -> dict[str, object]:
    lineup, target, origin, video = row
    video_output = None
    if video is not None:
        video_output = {
            "id": str(video.id),
            "status": str(video.status.value if hasattr(video.status, "value") else video.status),
            "duration_ms": video.duration_ms if video.status == VideoStatus.ready else None,
            "size_bytes": video.size_bytes if video.status == VideoStatus.ready else None,
            "rights_note": video.rights_note,
            "failure_reason": video.failure_reason,
            "url": _media_url(video),
            "poster_url": _media_url(video, poster=True),
        }
    return {
        "id": str(lineup.id),
        "status": lineup.status.value if hasattr(lineup.status, "value") else lineup.status,
        "is_default": lineup.is_default,
        "default_needs_review": lineup.default_needs_review,
        "sort_order": lineup.sort_order,
        "created_at": lineup.created_at,
        "updated_at": lineup.updated_at,
        "target": {"id": str(target.id), "slug": target.slug, "name_zh": target.name_zh},
        "origin": {"id": str(origin.id), "slug": origin.slug, "name_zh": origin.name_zh},
        "steps": {
            "setup": lineup.setup_text,
            "aim": lineup.aim_text,
            "throw": lineup.throw_text,
            "result": lineup.result_text,
        },
        "check_info": {
            "last_checked_at": lineup.last_checked_at,
            "game_version_note": lineup.game_version_note,
        },
        "primary_video": video_output,
    }


@router.get("/lineups", response_model=list[AdminLineupOut])
async def list_lineups(
    status_filter: Literal["draft", "published", "needs_revalidation", "archived", "all"] = Query(
        "all", alias="status"
    ),
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> list[dict[str, object]]:
    statement = (
        select(Lineup, Target, Origin, TutorialVideo)
        .join(Target, Target.id == Lineup.target_id)
        .join(Origin, Origin.id == Lineup.origin_id)
        .outerjoin(TutorialVideo, TutorialVideo.lineup_id == Lineup.id)
    )
    if status_filter != "all":
        statement = statement.where(Lineup.status == status_filter)
    statement = statement.order_by(
        _lineup_status_order(), Lineup.sort_order, Lineup.updated_at.desc(), Lineup.id
    )
    return [_admin_lineup_output(row) for row in (await session.execute(statement)).all()]


@router.post("/lineups", response_model=AdminLineupOut, status_code=201)
async def create_lineup(
    body: LineupCreate,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    async with session.begin():
        await _content_lock(session)
        if await session.scalar(select(func.count()).select_from(Lineup)) >= 500:
            raise AppError(409, "CATALOG_LIMIT_REACHED", "目录已达到当前阶段上限。")
        target = await session.get(Target, body.target_id)
        origin = await session.get(Origin, body.origin_id)
        if target is None:
            raise AppError(404, "ADMIN_TARGET_NOT_FOUND", "Target 不存在。")
        if origin is None:
            raise AppError(404, "ADMIN_ORIGIN_NOT_FOUND", "Origin 不存在。")
        if not target.is_active or not origin.is_active:
            raise AppError(409, "POINT_INACTIVE", "Target 或 Origin 已停用。")
        lineup = Lineup(
            target_id=target.id,
            origin_id=origin.id,
            setup_text=body.steps.setup,
            aim_text=body.steps.aim,
            throw_text=body.steps.throw,
            result_text=body.steps.result,
            sort_order=body.sort_order,
            status=LineupStatus.draft,
            is_default=False,
            default_needs_review=False,
            last_checked_at=body.check_info.last_checked_at,
            game_version_note=body.check_info.game_version_note,
        )
        session.add(lineup)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise _integrity_error(exc, "lineup") from None
        add_admin_action(session, request.state.request_id)
        row = (lineup, target, origin, None)
    return _admin_lineup_output(row)


@router.patch("/lineups/{lineup_id}", response_model=AdminLineupOut)
async def patch_lineup(
    lineup_id: UUID,
    body: LineupPatch,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    if "steps" in body.model_fields_set and body.steps is None:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "请求参数不合法。",
            {"fields": [{"path": "steps", "reason": "invalid_value"}]},
        )
    if "check_info" in body.model_fields_set and body.check_info is None:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "请求参数不合法。",
            {"fields": [{"path": "check_info", "reason": "invalid_value"}]},
        )
    async with session.begin():
        await _content_lock(session)
        lineup = await session.get(Lineup, lineup_id, with_for_update=True)
        if lineup is None:
            raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
        if lineup.status == LineupStatus.archived:
            raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
        if lineup.status == LineupStatus.published and (
            "steps" in body.model_fields_set or "check_info" in body.model_fields_set
        ):
            raise AppError(409, "PUBLISHED_LINEUP_IMMUTABLE", "已发布路线须先下架后修改。")
        changed = False
        if "sort_order" in body.model_fields_set and body.sort_order is not None:
            changed |= lineup.sort_order != body.sort_order
            lineup.sort_order = body.sort_order
        if "steps" in body.model_fields_set and body.steps is not None:
            values = body.steps
            steps_changed = (
                lineup.setup_text,
                lineup.aim_text,
                lineup.throw_text,
                lineup.result_text,
            ) != (
                values.setup,
                values.aim,
                values.throw,
                values.result,
            )
            lineup.setup_text, lineup.aim_text, lineup.throw_text, lineup.result_text = (
                values.setup,
                values.aim,
                values.throw,
                values.result,
            )
            changed |= steps_changed
            if steps_changed:
                lineup.last_checked_at = None
        if "check_info" in body.model_fields_set and body.check_info is not None:
            changed |= (lineup.last_checked_at, lineup.game_version_note) != (
                body.check_info.last_checked_at,
                body.check_info.game_version_note,
            )
            lineup.last_checked_at = body.check_info.last_checked_at
            lineup.game_version_note = body.check_info.game_version_note
        if changed:
            add_admin_action(session, request.state.request_id)
        await session.flush()
        row = await _lineup_row(session, lineup_id)
    return _admin_lineup_output(row)


def _safe_public_file(video: TutorialVideo) -> bool:
    from app.services.media import public_artifacts_look_valid

    return public_artifacts_look_valid(video)


@router.post("/lineups/{lineup_id}/publish", response_model=AdminLineupOut)
async def publish_lineup(
    lineup_id: UUID,
    body: PublishRequest,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    from app.services.media import MediaLockTimeout, media_lock

    try:
        async with media_lock(timeout_seconds=5):
            return await _publish_lineup_locked(lineup_id, body, request, session)
    except MediaLockTimeout:
        raise AppError(409, "MEDIA_BUSY", "媒体处理繁忙，请稍后重试。") from None


async def _publish_lineup_locked(
    lineup_id: UUID, body: PublishRequest, request: Request, session: AsyncSession
) -> dict[str, object]:
    async with session.begin():
        await _content_lock(session)
        row = await _lineup_row(session, lineup_id)
        if row is None:
            raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
        lineup, target, origin, video = row
        if lineup.status == LineupStatus.archived:
            raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
        if lineup.status == LineupStatus.published:
            return _admin_lineup_output(row)
        missing: list[str] = []
        for field, value in (
            ("steps.setup", lineup.setup_text),
            ("steps.aim", lineup.aim_text),
            ("steps.throw", lineup.throw_text),
            ("steps.result", lineup.result_text),
        ):
            if not value.strip():
                missing.append(field)
        if video is None or video.status != VideoStatus.ready or not _safe_public_file(video):
            missing.append("primary_video")
        elif not video.rights_note.strip():
            missing.append("primary_video.rights_note")
        if not target.is_active:
            missing.append("target")
        if not origin.is_active:
            missing.append("origin")
        if get_map_catalog().asset_status == "approved":
            for field, value in (
                ("target.map_x", target.map_x),
                ("target.map_y", target.map_y),
            ):
                if value is None:
                    missing.append(field)
        if missing:
            raise AppError(
                409, "LINEUP_NOT_PUBLISHABLE", "路线尚未满足发布条件。", {"missing": missing}
            )
        published_count = await session.scalar(
            select(func.count())
            .select_from(Lineup)
            .where(
                Lineup.target_id == lineup.target_id,
                Lineup.origin_id == lineup.origin_id,
                Lineup.status == LineupStatus.published,
            )
        )
        if not published_count and not body.make_default:
            raise AppError(409, "DEFAULT_REQUIRED", "同组第一条路线发布时必须设为默认。")
        if body.make_default:
            await session.execute(
                sa.update(Lineup)
                .where(
                    Lineup.target_id == lineup.target_id,
                    Lineup.origin_id == lineup.origin_id,
                    Lineup.id != lineup.id,
                )
                .values(is_default=False, default_needs_review=False)
            )
        lineup.status = LineupStatus.published
        lineup.is_default = body.make_default
        lineup.default_needs_review = False
        add_admin_action(session, request.state.request_id)
        await session.flush()
        row = await _lineup_row(session, lineup_id)
    return _admin_lineup_output(row)


async def _lineup_action(
    lineup_id: UUID,
    action: Literal["set_default", "unpublish", "needs_revalidation"],
    request: Request,
    session: AsyncSession,
) -> dict[str, object]:
    async with session.begin():
        await _content_lock(session)
        row = await _lineup_row(session, lineup_id)
        if row is None:
            raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
        lineup, _target, _origin, _video = row
        if lineup.status == LineupStatus.archived:
            raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
        if action == "set_default":
            if lineup.status != LineupStatus.published:
                raise AppError(409, "LINEUP_NOT_PUBLISHED", "只有已发布路线可以设为默认。")
            if lineup.is_default and not lineup.default_needs_review:
                return _admin_lineup_output(row)
            await session.execute(
                sa.update(Lineup)
                .where(
                    Lineup.target_id == lineup.target_id,
                    Lineup.origin_id == lineup.origin_id,
                    Lineup.id != lineup.id,
                )
                .values(is_default=False, default_needs_review=False)
            )
            lineup.is_default = True
            lineup.default_needs_review = False
        else:
            if action == "unpublish":
                desired_status = LineupStatus.draft
            else:
                desired_status = LineupStatus.needs_revalidation
            if lineup.status == desired_status:
                return _admin_lineup_output(row)
            if lineup.is_default:
                siblings = await session.scalar(
                    select(func.count())
                    .select_from(Lineup)
                    .where(
                        Lineup.target_id == lineup.target_id,
                        Lineup.origin_id == lineup.origin_id,
                        Lineup.status == LineupStatus.published,
                        Lineup.id != lineup.id,
                    )
                )
                if siblings:
                    raise AppError(
                        409,
                        "DEFAULT_REPLACEMENT_REQUIRED",
                        "请先在另一条已发布路线设为默认。",
                    )
            lineup.status = desired_status
            lineup.is_default = False
            lineup.default_needs_review = False
            if desired_status == LineupStatus.needs_revalidation:
                lineup.last_checked_at = None
        add_admin_action(session, request.state.request_id)
        await session.flush()
        return _admin_lineup_output(await _lineup_row(session, lineup_id))


@router.post("/lineups/{lineup_id}/set-default", response_model=AdminLineupOut)
async def set_default_lineup(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _lineup_action(lineup_id, "set_default", request, session)


@router.post("/lineups/{lineup_id}/unpublish", response_model=AdminLineupOut)
async def unpublish_lineup(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _lineup_action(lineup_id, "unpublish", request, session)


@router.post("/lineups/{lineup_id}/mark-needs-revalidation", response_model=AdminLineupOut)
async def mark_lineup_needs_revalidation(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _lineup_action(lineup_id, "needs_revalidation", request, session)


async def _archive_lineup(
    lineup_id: UUID, request: Request, session: AsyncSession
) -> dict[str, object]:
    from app.services.media import MediaLockTimeout, media_lock, move_public_pair_to_archive

    moved_paths: tuple[str | None, str | None] | None = None
    try:
        async with media_lock(timeout_seconds=5):
            try:
                async with session.begin():
                    await _content_lock(session)
                    row = await _lineup_row(session, lineup_id)
                    if row is None:
                        raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
                    lineup, _target, _origin, video = row
                    if lineup.status == LineupStatus.archived:
                        return _admin_lineup_output(row)
                    if lineup.status == LineupStatus.published:
                        raise AppError(409, "LINEUP_STILL_PUBLISHED", "请先下架路线，再归档。")
                    if video is not None and video.status == VideoStatus.ready:
                        moved_paths = (video.relative_path, video.poster_relative_path)
                        try:
                            await move_public_pair_to_archive(moved_paths, assume_locked=True)
                        except OSError:
                            raise AppError(
                                500,
                                "INTERNAL_ERROR",
                                "服务暂时异常，请凭请求编号重试。",
                            ) from None
                        video.status = VideoStatus.archived
                    lineup.status = LineupStatus.archived
                    lineup.is_default = False
                    lineup.default_needs_review = False
                    add_admin_action(session, request.state.request_id)
                    await session.flush()
                    return _admin_lineup_output(await _lineup_row(session, lineup_id))
            except BaseException:
                if moved_paths is not None:
                    from app.services.media import move_archived_pair_to_public

                    try:
                        await move_archived_pair_to_public(moved_paths, assume_locked=True)
                    except OSError:
                        pass
                raise
    except MediaLockTimeout:
        raise AppError(409, "MEDIA_BUSY", "媒体处理繁忙，请稍后重试。") from None


@router.post("/lineups/{lineup_id}/archive", response_model=AdminLineupOut)
async def archive_lineup(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _archive_lineup(lineup_id, request, session)


async def _restore_lineup(
    lineup_id: UUID, request: Request, session: AsyncSession
) -> dict[str, object]:
    async with session.begin():
        await _content_lock(session)
        row = await _lineup_row(session, lineup_id)
        if row is None:
            raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
        lineup, target, origin, _video = row
        if lineup.status != LineupStatus.archived:
            return _admin_lineup_output(row)
        if not target.is_active or not origin.is_active:
            raise AppError(409, "POINT_INACTIVE", "Target 或 Origin 已停用。")
        lineup.status = LineupStatus.draft
        lineup.is_default = False
        lineup.default_needs_review = False
        add_admin_action(session, request.state.request_id)
        await session.flush()
        return _admin_lineup_output(await _lineup_row(session, lineup_id))


@router.post("/lineups/{lineup_id}/restore", response_model=AdminLineupOut)
async def restore_lineup(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _restore_lineup(lineup_id, request, session)


async def _video_archive(
    lineup_id: UUID, request: Request, session: AsyncSession
) -> dict[str, object]:
    from app.services.media import MediaLockTimeout, media_lock, move_public_pair_to_archive

    moved_paths: tuple[str | None, str | None] | None = None
    try:
        async with media_lock(timeout_seconds=5):
            try:
                async with session.begin():
                    await _content_lock(session)
                    row = await _lineup_row(session, lineup_id)
                    if row is None:
                        raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
                    lineup, _target, _origin, video = row
                    if lineup.status == LineupStatus.published:
                        raise AppError(409, "LINEUP_STILL_PUBLISHED", "请先下架路线，再归档媒体。")
                    if video is None:
                        raise AppError(404, "ADMIN_VIDEO_NOT_FOUND", "主视频不存在。")
                    if video.status != VideoStatus.ready:
                        return _admin_lineup_output(row)
                    moved_paths = (video.relative_path, video.poster_relative_path)
                    try:
                        await move_public_pair_to_archive(moved_paths, assume_locked=True)
                    except OSError:
                        raise AppError(
                            500,
                            "INTERNAL_ERROR",
                            "服务暂时异常，请凭请求编号重试。",
                        ) from None
                    video.status = VideoStatus.archived
                    add_admin_action(session, request.state.request_id)
                    await session.flush()
                    return _admin_lineup_output(await _lineup_row(session, lineup_id))
            except BaseException:
                if moved_paths is not None:
                    from app.services.media import move_archived_pair_to_public

                    try:
                        await move_archived_pair_to_public(moved_paths, assume_locked=True)
                    except OSError:
                        pass
                raise
    except MediaLockTimeout:
        raise AppError(409, "MEDIA_BUSY", "媒体处理繁忙，请稍后重试。") from None


@router.post("/lineups/{lineup_id}/video/archive", response_model=AdminLineupOut)
async def archive_video(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _video_archive(lineup_id, request, session)


async def _video_restore(
    lineup_id: UUID, request: Request, session: AsyncSession
) -> dict[str, object]:
    from app.services.media import (
        MediaLockTimeout,
        archived_artifacts_look_valid,
        media_lock,
        move_archived_pair_to_public,
    )

    moved_paths: tuple[str | None, str | None] | None = None
    try:
        async with media_lock(timeout_seconds=5):
            row = await _lineup_row(session, lineup_id)
            if row is None:
                await session.rollback()
                raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
            lineup, _target, _origin, video = row
            if lineup.status == LineupStatus.archived:
                await session.rollback()
                raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
            if video is None:
                await session.rollback()
                raise AppError(404, "ADMIN_VIDEO_NOT_FOUND", "主视频不存在。")
            if video.status == VideoStatus.ready:
                output = _admin_lineup_output(row)
                await session.rollback()
                return output
            if video.status != VideoStatus.archived:
                await session.rollback()
                raise AppError(409, "ARCHIVED_MEDIA_INVALID", "归档媒体无法验证，暂不能恢复。")
            snapshot_key = (
                video.id,
                video.storage_key,
                video.relative_path,
                video.poster_relative_path,
                video.duration_ms,
                video.size_bytes,
                video.checksum_sha256,
            )
            snapshot = SimpleNamespace(
                relative_path=video.relative_path,
                poster_relative_path=video.poster_relative_path,
                duration_ms=video.duration_ms,
                size_bytes=video.size_bytes,
                checksum_sha256=video.checksum_sha256,
            )
            await session.rollback()
            if not await archived_artifacts_look_valid(snapshot):
                raise AppError(409, "ARCHIVED_MEDIA_INVALID", "归档媒体无法验证，暂不能恢复。")
            try:
                async with session.begin():
                    await _content_lock(session)
                    row = await _lineup_row(session, lineup_id)
                    if row is None:
                        raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
                    lineup, _target, _origin, video = row
                    if lineup.status == LineupStatus.archived:
                        raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
                    if video is None:
                        raise AppError(404, "ADMIN_VIDEO_NOT_FOUND", "主视频不存在。")
                    if video.status == VideoStatus.ready:
                        return _admin_lineup_output(row)
                    if (
                        video.status != VideoStatus.archived
                        or (
                            video.id,
                            video.storage_key,
                            video.relative_path,
                            video.poster_relative_path,
                            video.duration_ms,
                            video.size_bytes,
                            video.checksum_sha256,
                        )
                        != snapshot_key
                    ):
                        raise AppError(
                            409, "ARCHIVED_MEDIA_INVALID", "归档媒体无法验证，暂不能恢复。"
                        )
                    moved_paths = (video.relative_path, video.poster_relative_path)
                    try:
                        await move_archived_pair_to_public(moved_paths, assume_locked=True)
                    except OSError:
                        raise AppError(
                            409, "ARCHIVED_MEDIA_INVALID", "归档媒体无法验证，暂不能恢复。"
                        ) from None
                    video.status = VideoStatus.ready
                    lineup.last_checked_at = None
                    add_admin_action(session, request.state.request_id)
                    await session.flush()
                    return _admin_lineup_output(await _lineup_row(session, lineup_id))
            except BaseException:
                if moved_paths is not None:
                    from app.services.media import move_public_pair_to_archive

                    try:
                        await move_public_pair_to_archive(moved_paths, assume_locked=True)
                    except OSError:
                        pass
                raise
    except MediaLockTimeout:
        raise AppError(409, "MEDIA_BUSY", "媒体处理繁忙，请稍后重试。") from None


@router.post("/lineups/{lineup_id}/video/restore", response_model=AdminLineupOut)
async def restore_video(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    return await _video_restore(lineup_id, request, session)


def _video_error(exc) -> AppError:
    return AppError(exc.status_code, exc.code, exc.message)


async def _mark_failed_video(
    session: AsyncSession,
    lineup_id: UUID,
    rights_note: str,
    failure_reason: str,
    request_id: str,
) -> None:
    async with session.begin():
        await _content_lock(session)
        lineup = await session.get(Lineup, lineup_id, with_for_update=True)
        if lineup is None or lineup.status in {LineupStatus.published, LineupStatus.archived}:
            return
        video = (
            await session.execute(
                select(TutorialVideo).where(TutorialVideo.lineup_id == lineup_id).with_for_update()
            )
        ).scalar_one_or_none()
        if video is not None and video.status == VideoStatus.ready:
            return
        if video is None:
            video = TutorialVideo(
                lineup_id=lineup_id,
                status=VideoStatus.failed,
                rights_note=rights_note,
                failure_reason=failure_reason,
            )
            session.add(video)
        else:
            video.status = VideoStatus.failed
            video.storage_key = None
            video.relative_path = None
            video.poster_relative_path = None
            video.duration_ms = None
            video.size_bytes = None
            video.checksum_sha256 = None
            video.rights_note = rights_note
            video.failure_reason = failure_reason
        add_admin_action(session, request_id)


@router.post("/lineups/{lineup_id}/video", response_model=AdminLineupOut)
async def upload_video(
    lineup_id: UUID,
    request: Request,
    _username: str = _ADMIN_DEPENDENCY,
    session: AsyncSession = _SESSION_DEPENDENCY,
) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type != "multipart/form-data":
        raise AppError(415, "UNSUPPORTED_CONTENT_TYPE", "请求内容类型不受支持。")
    form = None
    try:
        try:
            form = await request.form(max_files=1, max_fields=1)
        except (MultiPartException, StarletteHTTPException):
            # Starlette converts parser limit failures to an HTTPException
            # once the request is attached to an app.  Keep both parser paths
            # on the same stable validation contract without exposing parser
            # text or accepting an unbounded multipart body.
            raise _validation_error({"path": "file", "reason": "too_many_parts"}) from None
        items = list(form.multi_items())
        file_items = [value for key, value in items if key == "file"]
        rights_items = [value for key, value in items if key == "rights_note"]
        validation_fields: list[dict[str, str]] = []
        if len(file_items) == 0:
            validation_fields.append({"path": "file", "reason": "missing"})
        elif len(file_items) > 1:
            validation_fields.append({"path": "file", "reason": "too_many"})
        elif not isinstance(file_items[0], StarletteUploadFile):
            validation_fields.append({"path": "file", "reason": "invalid_type"})
        if len(rights_items) == 0:
            validation_fields.append({"path": "rights_note", "reason": "missing"})
        elif len(rights_items) > 1:
            validation_fields.append({"path": "rights_note", "reason": "too_many"})
        elif not isinstance(rights_items[0], str):
            validation_fields.append({"path": "rights_note", "reason": "invalid_type"})
        unexpected = [key for key, _value in items if key not in {"file", "rights_note"}]
        if unexpected:
            validation_fields.append({"path": "request", "reason": "unexpected_field"})
        if validation_fields:
            raise _validation_error(*validation_fields)
        upload = file_items[0]
        rights_note = rights_items[0]
        stripped_rights_note = rights_note.strip()
        if any(
            unicodedata.category(char) == "Cc" or char in {"\u2028", "\u2029"}
            for char in rights_note
        ):
            raise _validation_error({"path": "rights_note", "reason": "invalid_text"})
        if not 1 <= len(stripped_rights_note) <= 500:
            reason = "string_too_short" if not stripped_rights_note else "string_too_long"
            raise _validation_error({"path": "rights_note", "reason": reason})
        lineup = await session.get(Lineup, lineup_id)
        if lineup is None:
            raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
        if lineup.status == LineupStatus.archived:
            raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
        if lineup.status == LineupStatus.published:
            raise AppError(409, "PUBLISHED_LINEUP_IMMUTABLE", "已发布路线须先下架后修改。")
        # The dependency session has an implicit read transaction after the
        # checks above.  Close it before the long-running media operation so the
        # callback can open the required short media-lock -> DB-lock transaction.
        await session.rollback()

        async def commit_processed(processed) -> tuple[str | None, str | None] | None:
            previous_paths = None
            async with session.begin():
                await _content_lock(session)
                current = await session.get(Lineup, lineup_id, with_for_update=True)
                if current is None:
                    raise AppError(404, "ADMIN_LINEUP_NOT_FOUND", "路线不存在。")
                if current.status == LineupStatus.archived:
                    raise AppError(409, "LINEUP_ARCHIVED", "归档路线须先恢复。")
                if current.status == LineupStatus.published:
                    raise AppError(409, "PUBLISHED_LINEUP_IMMUTABLE", "已发布路线须先下架后修改。")
                video = (
                    await session.execute(
                        select(TutorialVideo)
                        .where(TutorialVideo.lineup_id == lineup_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if video is None:
                    video = TutorialVideo(
                        lineup_id=lineup_id, status=VideoStatus.ready, rights_note=rights_note
                    )
                    session.add(video)
                elif video.status == VideoStatus.ready:
                    previous_paths = (video.relative_path, video.poster_relative_path)
                video.status = VideoStatus.ready
                video.storage_key = processed.storage_key
                video.relative_path = processed.relative_path
                video.poster_relative_path = processed.poster_relative_path
                video.duration_ms = processed.duration_ms
                video.size_bytes = processed.size_bytes
                video.checksum_sha256 = processed.checksum_sha256
                video.rights_note = rights_note.strip()
                video.failure_reason = None
                current.last_checked_at = None
                add_admin_action(session, request.state.request_id)
                await session.flush()
            return previous_paths

        try:
            from app.services.media import MediaError, process_upload

            await process_upload(
                upload,
                content_type=upload.content_type,
                rights_note=rights_note,
                lineup_id=lineup_id,
                request_id=request.state.request_id,
                commit=commit_processed,
            )
        except MediaError as exc:
            if exc.persist_failure:
                await _mark_failed_video(
                    session,
                    lineup_id,
                    rights_note,
                    exc.failure_reason or "processing_failed",
                    request.state.request_id,
                )
            raise _video_error(exc) from None
        async with get_session_factory()() as output_session:
            row = await _lineup_row(output_session, lineup_id)
        return _admin_lineup_output(row)
    finally:
        if form is not None:
            await form.close()
