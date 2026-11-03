"""Opt-in public-catalog metadata fixture for the existing 0008 loopback Web database."""

import argparse
import asyncio
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Lineup, LineupStatus, Origin, Target, TutorialVideo, VideoStatus


def identity(name):
    # Stable UUIDv4 representation for the formal command validator; no production IDs.
    raw = bytearray(uuid.uuid5(uuid.NAMESPACE_URL, "smokeline-selftest:" + name).bytes)
    raw[6] = (raw[6] & 15) | 64
    raw[8] = (raw[8] & 63) | 128
    return uuid.UUID(bytes=bytes(raw))


async def run(args):
    url = make_url(args.database_url)
    if url.host != "127.0.0.1" or url.port != 65402 or (
        url.database != "tactics_formal_selftest"
    ):
        raise RuntimeError("Only loopback:65402/tactics_formal_selftest is permitted")
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db, db.begin():
            await db.execute(sa.text("SELECT pg_advisory_xact_lock(1397575499)"))
            if args.command == "video":
                video = await db.scalar(sa.select(TutorialVideo).where(
                    TutorialVideo.lineup_id == args.lineup_id
                ))
                if video is None or args.lineup_id not in {
                    identity("visible"), identity("variant"), identity("draft")
                }:
                    raise RuntimeError("Only this script's isolated fixture IDs may be toggled")
                video.status = VideoStatus.ready if args.state == "ready" else VideoStatus.archived
                print(f"fixture_video {args.lineup_id} {args.state}")
                return
            target = await db.get(Target, identity("target"))
            if target is None:
                db.add(Target(id=identity("target"), slug="tactics-web-fixture-target",
                              name_zh="隔离教学目标", map_x=50, map_y=50))
                db.add(Origin(id=identity("origin"), slug="tactics-web-fixture-origin",
                              name_zh="隔离教学站位", map_x=25, map_y=25))
                await db.flush()
            for name in ["visible", "variant", "draft"]:
                if await db.get(Lineup, identity(name)) is not None:
                    continue
                status = LineupStatus.draft if name == "draft" else LineupStatus.published
                db.add(Lineup(id=identity(name), target_id=identity("target"),
                              origin_id=identity("origin"), is_default=name == "visible",
                              status=status,
                              setup_text=f"隔离{name}站位", aim_text="隔离瞄点",
                              throw_text="隔离投掷", result_text="隔离结果"))
                await db.flush()
                storage = identity("video-" + name)
                db.add(TutorialVideo(lineup_id=identity(name), status=VideoStatus.ready,
                                     storage_key=storage, relative_path=f"{storage}/fixture.mp4",
                                     poster_relative_path=f"{storage}/fixture.webp",
                                     duration_ms=1000,
                                     size_bytes=1024, checksum_sha256="a" * 64,
                                     rights_note="Selftest synthetic metadata; no media files"))
            print("target_slug tactics-web-fixture-target")
            for name in ["visible", "variant", "draft"]:
                print(f"{name} {identity(name)}")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    video = commands.add_parser("video")
    video.add_argument("lineup_id", type=uuid.UUID)
    video.add_argument("state", choices=["ready", "archived"])
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    if __import__("sys").platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
