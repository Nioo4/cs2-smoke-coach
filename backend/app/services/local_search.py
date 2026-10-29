"""The single public-visibility query and deterministic Mirage selection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, database_is_unavailable
from app.db.models import (
    Lineup,
    LineupStatus,
    Origin,
    OriginAlias,
    Target,
    TargetAlias,
    TutorialVideo,
    VideoStatus,
)
from app.db.session import get_session_factory
from app.services.normalization import CatalogTerm, catalog_terms


@dataclass(frozen=True, slots=True)
class PublicRow:
    lineup: Lineup
    target: Target
    origin: Origin
    video: TutorialVideo


@dataclass(frozen=True, slots=True)
class CatalogEntity:
    id: UUID
    slug: str
    name_zh: str
    name_en: str | None
    terms: tuple[CatalogTerm, ...]
    sort_order: int = 0
    updated_at: object | None = None


@asynccontextmanager
async def public_snapshot() -> AsyncIterator[AsyncSession]:
    """Yield one REPEATABLE READ, READ ONLY transaction for public reads."""

    async with get_session_factory()() as session:
        try:
            async with session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                await session.execute(text("SET TRANSACTION READ ONLY"))
                yield session
        except DBAPIError as exc:
            if database_is_unavailable(exc):
                raise AppError(
                    503, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试。"
                ) from None
            raise


def _available_predicate() -> tuple[sa.ColumnElement[bool], ...]:
    return (
        Lineup.status == LineupStatus.published,
        Target.is_active.is_(True),
        Origin.is_active.is_(True),
        TutorialVideo.status == VideoStatus.ready,
        TutorialVideo.storage_key.is_not(None),
        TutorialVideo.relative_path.is_not(None),
        TutorialVideo.poster_relative_path.is_not(None),
        TutorialVideo.duration_ms.is_not(None),
        TutorialVideo.size_bytes.is_not(None),
        TutorialVideo.checksum_sha256.is_not(None),
        func.length(func.btrim(Lineup.setup_text)) > 0,
        func.length(func.btrim(Lineup.aim_text)) > 0,
        func.length(func.btrim(Lineup.throw_text)) > 0,
        func.length(func.btrim(Lineup.result_text)) > 0,
    )


async def available_rows(
    session: AsyncSession,
    *,
    target_id: UUID | None = None,
    origin_id: UUID | None = None,
) -> list[PublicRow]:
    statement = (
        select(Lineup, Target, Origin, TutorialVideo)
        .join(Target, Target.id == Lineup.target_id)
        .join(Origin, Origin.id == Lineup.origin_id)
        .join(TutorialVideo, TutorialVideo.lineup_id == Lineup.id)
        .where(*_available_predicate())
    )
    if target_id is not None:
        statement = statement.where(Lineup.target_id == target_id)
    if origin_id is not None:
        statement = statement.where(Lineup.origin_id == origin_id)
    result = await session.execute(statement)
    # PostgreSQL btrim() above removes ordinary spaces only. Keep the same
    # Unicode-whitespace definition as the publish gate for imported/faulted
    # records containing only newlines or other whitespace.
    return [
        PublicRow(*row)
        for row in result.all()
        if all(
            value.strip()
            for value in (row[0].setup_text, row[0].aim_text, row[0].throw_text, row[0].result_text)
        )
    ]


async def active_catalog(
    session: AsyncSession,
) -> tuple[list[CatalogEntity], list[CatalogEntity]]:
    targets = (
        (await session.execute(select(Target).where(Target.is_active.is_(True)))).scalars().all()
    )
    origins = (
        (await session.execute(select(Origin).where(Origin.is_active.is_(True)))).scalars().all()
    )
    target_alias_rows = (
        (
            await session.execute(
                select(TargetAlias).where(TargetAlias.target_id.in_([item.id for item in targets]))
            )
        )
        .scalars()
        .all()
        if targets
        else []
    )
    origin_alias_rows = (
        (
            await session.execute(
                select(OriginAlias).where(OriginAlias.origin_id.in_([item.id for item in origins]))
            )
        )
        .scalars()
        .all()
        if origins
        else []
    )
    target_aliases: dict[UUID, list[str]] = {}
    for item in target_alias_rows:
        target_aliases.setdefault(item.target_id, []).append(item.alias)
    origin_aliases: dict[UUID, list[str]] = {}
    for item in origin_alias_rows:
        origin_aliases.setdefault(item.origin_id, []).append(item.alias)

    def target_entity(item: Target) -> CatalogEntity:
        return CatalogEntity(
            item.id,
            item.slug,
            item.name_zh,
            item.name_en,
            catalog_terms(item.name_zh, item.name_en, target_aliases.get(item.id, [])),
            item.sort_order,
            getattr(item, "updated_at", None),
        )

    def origin_entity(item: Origin) -> CatalogEntity:
        return CatalogEntity(
            item.id,
            item.slug,
            item.name_zh,
            item.name_en,
            catalog_terms(item.name_zh, item.name_en, origin_aliases.get(item.id, [])),
            item.sort_order,
            getattr(item, "updated_at", None),
        )

    return (
        [target_entity(item) for item in targets],
        [origin_entity(item) for item in origins],
    )


def media_url(row: PublicRow, *, poster: bool = False) -> str:
    path = row.video.poster_relative_path if poster else row.video.relative_path
    checksum = row.video.checksum_sha256 or ""
    return f"/media/{path}?v={checksum[:12]}"


def point_summary(point: Target | Origin) -> dict[str, object]:
    return {
        "slug": point.slug,
        "name_zh": point.name_zh,
        "map_x": None if point.map_x is None else float(point.map_x),
        "map_y": None if point.map_y is None else float(point.map_y),
    }


def _hint_fragment(value: str) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= 60 else f"{compact[:60]}…"


def variant_hint(lineup: Lineup) -> str:
    return f"站位：{_hint_fragment(lineup.setup_text)}；瞄点：{_hint_fragment(lineup.aim_text)}"


def primary_video(row: PublicRow) -> dict[str, object]:
    return {
        "url": media_url(row),
        "poster_url": media_url(row, poster=True),
        "duration_ms": row.video.duration_ms,
    }


def lineup_summary(row: PublicRow) -> dict[str, object]:
    return {
        "id": str(row.lineup.id),
        "is_default": bool(row.lineup.is_default),
        "variant_hint": variant_hint(row.lineup),
        "primary_video": primary_video(row),
    }


def route_choice(row: PublicRow) -> dict[str, object]:
    return {
        "target": point_summary(row.target),
        "origin": point_summary(row.origin),
        "lineup": lineup_summary(row),
    }


def _row_sort_key(row: PublicRow) -> tuple[int, int, str]:
    return (
        0 if row.lineup.is_default else 1,
        row.lineup.sort_order,
        str(row.lineup.id),
    )


def route_sort_key(row: PublicRow) -> tuple[int, str, str, int, str, str]:
    return (
        row.target.sort_order,
        row.target.name_zh,
        str(row.target.id),
        row.origin.sort_order,
        row.origin.name_zh,
        str(row.origin.id),
    )


def choose_group(rows: list[PublicRow]) -> PublicRow:
    if not rows:
        raise LookupError("empty group")
    defaults = [row for row in rows if row.lineup.is_default]
    if len(defaults) != 1:
        raise AppError(503, "CONTENT_CONFIGURATION_ERROR", "公开内容配置暂时异常，请稍后重试。")
    return defaults[0]


def detail_from_row(row: PublicRow, group_rows: list[PublicRow]) -> dict[str, object]:
    others = sorted(
        (candidate for candidate in group_rows if candidate.lineup.id != row.lineup.id),
        key=_row_sort_key,
    )
    checked = row.lineup.last_checked_at
    return {
        "id": str(row.lineup.id),
        "target": point_summary(row.target),
        "origin": point_summary(row.origin),
        "is_default": bool(row.lineup.is_default),
        "source": "local_curated",
        "steps": {
            "setup": row.lineup.setup_text,
            "aim": row.lineup.aim_text,
            "throw": row.lineup.throw_text,
            "result": row.lineup.result_text,
        },
        "check_info": {
            "last_checked_at": checked.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if checked
            else None,
            "game_version_note": row.lineup.game_version_note,
        },
        "primary_video": primary_video(row),
        "other_lineups": [lineup_summary(candidate) for candidate in others],
    }


def group_rows(rows: list[PublicRow]) -> dict[tuple[UUID, UUID], list[PublicRow]]:
    groups: dict[tuple[UUID, UUID], list[PublicRow]] = {}
    for row in rows:
        groups.setdefault((row.target.id, row.origin.id), []).append(row)
    return groups
