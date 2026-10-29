"""Public Mirage map, detail, and single-graph search endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Request
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.core.errors import AppError
from app.core.routing import JsonBodyRoute
from app.db.models import Target
from app.schemas.public import (
    SLUG_PATTERN,
    LineupDetail,
    MapResponse,
    SearchRequest,
    SearchResponse,
    TargetChoices,
    TranscriptionResponse,
)
from app.services.agent_graph import (
    run_search,
)
from app.services.audio import MAX_AUDIO_BYTES, transcribe_upload
from app.services.local_search import (
    PublicRow,
    available_rows,
    choose_group,
    detail_from_row,
    group_rows,
    lineup_summary,
    point_summary,
    public_snapshot,
)
from app.services.map_catalog import get_map_catalog

router = APIRouter(prefix="/api/v1", tags=["public"], route_class=JsonBodyRoute)


def _request_event(
    request: Request,
    *,
    response_type: str | None = None,
    target_slug: str | None = None,
    origin_slug: str | None = None,
    error_code: str | None = None,
) -> None:
    request.state.public_event = {
        "response_type": response_type,
        "target_slug": target_slug,
        "origin_slug": origin_slug,
        "error_code": error_code,
    }


@router.get("/maps/mirage", response_model=MapResponse)
async def map_summary(request: Request) -> dict[str, object]:
    map_data = get_map_catalog()
    async with public_snapshot() as session:
        rows = await available_rows(session)
        if map_data.asset_status == "approved" and any(
            row.target.map_x is None or row.target.map_y is None for row in rows
        ):
            _request_event(request, error_code="CONTENT_CONFIGURATION_ERROR")
            raise AppError(503, "CONTENT_CONFIGURATION_ERROR", "公开内容配置暂时异常，请稍后重试。")
    targets: dict[uuid.UUID, tuple[Target, set[uuid.UUID]]] = {}
    for row in rows:
        target, origins = targets.setdefault(row.target.id, (row.target, set()))
        origins.add(row.origin.id)
    sorted_targets = sorted(
        targets.values(),
        key=lambda item: (item[0].sort_order, item[0].name_zh, str(item[0].id)),
    )
    _request_event(request, response_type=None)
    return {
        "map": {
            "slug": map_data.slug,
            "name_zh": map_data.name_zh,
            "asset_url": map_data.asset_url,
            "asset_status": map_data.asset_status,
        },
        "published_lineup_count": len(rows),
        "targets": [
            {
                "slug": target.slug,
                "name_zh": target.name_zh,
                "map_x": None if target.map_x is None else float(target.map_x),
                "map_y": None if target.map_y is None else float(target.map_y),
                "origin_count": len(origins),
            }
            for target, origins in sorted_targets
        ],
    }


@router.get("/targets/{target_slug}/lineups", response_model=TargetChoices)
async def target_lineups(
    target_slug: Annotated[str, Path(min_length=1, max_length=64, pattern=SLUG_PATTERN)],
    request: Request,
) -> dict[str, object]:
    async with public_snapshot() as session:
        rows = await available_rows(session)
    matching = [row for row in rows if row.target.slug == target_slug]
    if not matching:
        _request_event(request, error_code="TARGET_NOT_FOUND")
        raise AppError(404, "TARGET_NOT_FOUND", "目标位置不存在或暂不可用。")
    groups = group_rows(matching)
    choices: list[PublicRow] = []
    for key, group in groups.items():
        if key[0] == matching[0].target.id:
            choices.append(choose_group(group))
    choices.sort(
        key=lambda item: (item.origin.sort_order, item.origin.name_zh, str(item.origin.id))
    )
    # Spec 11.6: response_counts only aggregates POST /search responses. A
    # read-only route directory lookup is not itself a search response.
    _request_event(request, response_type=None, target_slug=target_slug)
    return {
        "target": point_summary(matching[0].target),
        "origins": [
            {"origin": point_summary(row.origin), "lineup": lineup_summary(row)} for row in choices
        ],
    }


@router.get("/lineups/{lineup_id}", response_model=LineupDetail)
async def public_lineup_detail(lineup_id: uuid.UUID, request: Request) -> dict[str, object]:
    async with public_snapshot() as session:
        rows = await available_rows(session)
    current = next((row for row in rows if row.lineup.id == lineup_id), None)
    if current is None:
        _request_event(request, error_code="LINEUP_NOT_FOUND")
        raise AppError(404, "LINEUP_NOT_FOUND", "路线不存在或暂不可用。")
    group = [
        row
        for row in rows
        if row.target.id == current.target.id and row.origin.id == current.origin.id
    ]
    _request_event(
        request,
        # The detail endpoint is a read-only representation of a lineup, not a
        # second search response for metrics purposes.
        response_type=None,
        target_slug=current.target.slug,
        origin_slug=current.origin.slug,
    )
    return detail_from_row(current, group)


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> dict[str, object]:
    request.state.public_input_mode = "text" if body.kind == "selection" else body.input_mode
    return await run_search(request, body)


@router.post("/transcriptions", response_model=TranscriptionResponse)
async def transcriptions(request: Request) -> dict[str, object]:
    request.state.public_input_mode = "voice"
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type != "multipart/form-data":
        raise AppError(415, "UNSUPPORTED_CONTENT_TYPE", "请求内容类型不受支持。")
    deadline = time.monotonic() + 25.0
    try:
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            form = await request.form(
                max_files=1,
                max_fields=1,
                max_part_size=MAX_AUDIO_BYTES + 1,
            )
    except MultiPartException:
        raise AppError(422, "VALIDATION_ERROR", "请求参数不合法。") from None
    except StarletteHTTPException as exc:
        if exc.status_code != 400:
            raise
        raise AppError(422, "VALIDATION_ERROR", "请求参数不合法。") from None
    except TimeoutError:
        raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。") from None
    try:
        items = list(form.multi_items())
        if (
            len(items) != 1
            or items[0][0] != "audio"
            or not isinstance(items[0][1], StarletteUploadFile)
        ):
            raise AppError(422, "VALIDATION_ERROR", "请求参数不合法。")
        transcription, duration = await transcribe_upload(
            items[0][1],
            request_id=request.state.request_id,
            deadline=deadline,
        )
        _request_event(request, response_type=None)
        return {"text": transcription.text, "duration_seconds": duration}
    finally:
        await form.close()
