"""Seed only the isolated Phase 5C performance project with 20 ready routes."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.db.models import (
    Lineup,
    LineupStatus,
    Origin,
    Target,
    TutorialVideo,
    VideoStatus,
)
from app.db.session import get_session_factory
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

PREFIX = "phase5c-perf-"
COUNT = 20


async def seed() -> int:
    if os.environ.get("PHASE5C_PERF_SEED") != "1":
        raise RuntimeError("PERF_SEED_SCOPE_REQUIRED")
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("AI_PROVIDER") != "disabled"
    ):
        raise RuntimeError("PERF_SEED_NONPRODUCTION_REQUIRED")
    database_url = os.environ.get("DATABASE_URL", "")
    try:
        parsed_url = make_url(database_url)
    except Exception as exc:
        raise RuntimeError("PERF_SEED_DATABASE_SCOPE") from exc
    if (
        parsed_url.drivername != "postgresql+psycopg"
        or parsed_url.host != "postgres"
        or parsed_url.port not in {None, 5432}
        or parsed_url.database != "smokeline"
        or parsed_url.username != "smokeline"
        or os.environ.get("MEDIA_STORE_ROOT") != "/data/media-store"
    ):
        raise RuntimeError("PERF_SEED_DATABASE_SCOPE")
    settings = get_settings()
    async with get_session_factory()() as session:
        existing = (
            (
                await session.execute(
                    select(Target).where(Target.slug.like(f"{PREFIX}target-%"))
                )
            )
            .scalars()
            .all()
        )
        if len(existing) == COUNT:
            ready_default_count = await session.scalar(
                select(func.count(Lineup.id))
                .join(TutorialVideo, TutorialVideo.lineup_id == Lineup.id)
                .where(
                    Lineup.status == LineupStatus.published,
                    Lineup.is_default.is_(True),
                    Lineup.target_id.in_([item.id for item in existing]),
                    TutorialVideo.status == VideoStatus.ready,
                )
            )
            if ready_default_count != COUNT:
                raise RuntimeError("PERF_FIXTURES_PARTIAL")
            print(
                f"PERF_FIXTURES_PASS existing={COUNT} seeded=0 total={ready_default_count} ai=disabled"
            )
            return COUNT
        if existing:
            raise RuntimeError("PERF_FIXTURES_PARTIAL")
        source = (
            await session.execute(
                select(TutorialVideo)
                .where(TutorialVideo.status == VideoStatus.ready)
                .order_by(TutorialVideo.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            source is None
            or not source.relative_path
            or not source.poster_relative_path
        ):
            raise RuntimeError("PERF_READY_MEDIA_MISSING")
        media_root = Path(settings.media_store_root) / "public"
        media_path = media_root / source.relative_path
        if not media_path.is_file() or media_path.is_symlink():
            raise RuntimeError("PERF_READY_MEDIA_MISSING")
        checksum = hashlib.sha256(media_path.read_bytes()).hexdigest()
        if checksum != source.checksum_sha256:
            raise RuntimeError("PERF_READY_MEDIA_CHECKSUM")
        targets: list[Target] = []
        origins: list[Origin] = []
        lineups: list[Lineup] = []
        videos: list[TutorialVideo] = []
        for index in range(COUNT):
            target_id = uuid.uuid4()
            origin_id = uuid.uuid4()
            lineup_id = uuid.uuid4()
            target = Target(
                id=target_id,
                slug=f"{PREFIX}target-{index:02d}",
                name_zh=f"性能目标{index:02d}",
                name_en=f"Phase 5C target {index:02d}",
                description="隔离性能测试夹具",
                map_x=float(index + 1),
                map_y=float(index + 1),
                sort_order=1000 + index,
            )
            origin = Origin(
                id=origin_id,
                slug=f"{PREFIX}origin-{index:02d}",
                name_zh=f"性能起点{index:02d}",
                name_en=f"Phase 5C origin {index:02d}",
                description="隔离性能测试夹具",
                map_x=float(index + 1),
                map_y=float(index + 2),
                sort_order=1000 + index,
            )
            targets.append(target)
            origins.append(origin)
            lineup = Lineup(
                id=lineup_id,
                target_id=target_id,
                origin_id=origin_id,
                setup_text="性能测试站位",
                aim_text="性能测试瞄点",
                throw_text="性能测试投掷",
                result_text="性能测试结果",
                is_default=True,
                status=LineupStatus.published,
                sort_order=0,
            )
            lineups.append(lineup)
            videos.append(
                TutorialVideo(
                    id=uuid.uuid4(),
                    lineup_id=lineup_id,
                    status=VideoStatus.ready,
                    storage_key=uuid.uuid4(),
                    relative_path=source.relative_path,
                    poster_relative_path=source.poster_relative_path,
                    duration_ms=source.duration_ms,
                    size_bytes=source.size_bytes,
                    checksum_sha256=source.checksum_sha256,
                    rights_note="隔离性能测试复用已就绪媒体",
                )
            )
        session.add_all([*targets, *origins])
        await session.flush()
        session.add_all([*lineups, *videos])
        await session.commit()
    print(f"PERF_FIXTURES_PASS existing=0 seeded={COUNT} total={COUNT} ai=disabled")
    return COUNT


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(seed()) == COUNT else 1)
