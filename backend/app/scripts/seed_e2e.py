"""Create the isolated, fixed E2E fixture required by Spec section 12.5.

This module deliberately has no web entry point.  It is an explicit test-volume
operation: every guard is checked before opening a database session for a write
or creating a media directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from app.core.config import Settings, get_settings
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
from app.services.media import media_lock
from app.services.normalization import normalize_text

EXPECTED_APP_ENV = "test"
EXPECTED_SEED_FLAG = "1"
EXPECTED_PROVIDER = "fake"
EXPECTED_DATABASE_HOST = "postgres"
EXPECTED_DATABASE_PORT = 5432
EXPECTED_DATABASE_NAME = "smokeline"
EXPECTED_DATABASE_USER = "smokeline"
EXPECTED_MEDIA_ROOT = "/data/media-store"
FIXTURE_NAMESPACE = uuid.UUID("9b36c7d5-9e11-4e9c-9ba7-1c0c0b7a6f25")
FIXTURE_NOTE = "自动化测试夹具，非真实教学"
WAV_RELATIVE_PATH = "e2e/recording.wav"


class SeedGuardError(RuntimeError):
    """Raised when the explicit E2E-only environment contract is not met."""


class SeedError(RuntimeError):
    """Raised when fixture state, media generation, or the atomic seed fails."""


@dataclass(frozen=True, slots=True)
class LineupSpec:
    index: int
    target_slug: str
    origin_slug: str
    sort_order: int
    is_default: bool
    setup: str
    aim: str
    throw: str
    result: str


@dataclass(frozen=True, slots=True)
class MediaArtifact:
    lineup_id: uuid.UUID
    video_id: uuid.UUID
    storage_key: uuid.UUID
    staged_video: Path
    staged_poster: Path
    relative_path: str
    poster_relative_path: str
    duration_ms: int
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class FixtureSnapshot:
    targets: tuple[Target, ...]
    target_aliases: tuple[TargetAlias, ...]
    origins: tuple[Origin, ...]
    origin_aliases: tuple[OriginAlias, ...]
    lineups: tuple[Lineup, ...]
    videos: tuple[TutorialVideo, ...]


def _fixture_id(kind: str, value: str) -> uuid.UUID:
    return uuid.uuid5(FIXTURE_NAMESPACE, f"{kind}:{value}")


def _target_id(slug: str) -> uuid.UUID:
    return _fixture_id("target", slug)


def _origin_id(slug: str) -> uuid.UUID:
    return _fixture_id("origin", slug)


def _lineup_id(index: int) -> uuid.UUID:
    return _fixture_id("lineup", str(index))


def _video_id(index: int) -> uuid.UUID:
    return _fixture_id("video", str(index))


def _parse_seed_database_url(value: str, password: str) -> URL:
    try:
        parsed = make_url(value)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise SeedGuardError("DATABASE_URL is not a parseable PostgreSQL URL") from exc
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host != EXPECTED_DATABASE_HOST
        or parsed.port != EXPECTED_DATABASE_PORT
        or parsed.database != EXPECTED_DATABASE_NAME
        or parsed.username != EXPECTED_DATABASE_USER
        or parsed.password != password
        or parsed.query
    ):
        raise SeedGuardError("DATABASE_URL is outside the isolated PostgreSQL test scope")
    return parsed


def validate_seed_environment(settings: Settings, environ: Mapping[str, str] | None = None) -> URL:
    """Validate every seed guard without creating a DB engine or filesystem entry."""

    env = os.environ if environ is None else environ
    if env.get("APP_ENV") != EXPECTED_APP_ENV or settings.app_env != EXPECTED_APP_ENV:
        raise SeedGuardError("APP_ENV=test must be explicitly set")
    if env.get("E2E_SEED") != EXPECTED_SEED_FLAG:
        raise SeedGuardError("E2E_SEED=1 must be explicitly set")
    if env.get("AI_PROVIDER") != EXPECTED_PROVIDER or settings.ai_provider != EXPECTED_PROVIDER:
        raise SeedGuardError("AI_PROVIDER=fake is required for the test fixture")
    if env.get("MEDIA_STORE_ROOT") != EXPECTED_MEDIA_ROOT:
        raise SeedGuardError("MEDIA_STORE_ROOT must be /data/media-store")
    if settings.media_store_root != EXPECTED_MEDIA_ROOT:
        raise SeedGuardError("MEDIA_STORE_ROOT is outside the test volume")
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise SeedGuardError("DATABASE_URL must be explicitly set")
    return _parse_seed_database_url(database_url, settings.postgres_password_value)


def _safe_child(root: Path, relative: str) -> Path:
    """Return a non-link child that remains below root, or fail closed."""

    if not relative or "\\" in relative or relative.startswith("/"):
        raise SeedError("unsafe fixture relative path")
    components = relative.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise SeedError("unsafe fixture relative path")
    if root.is_symlink() or not root.is_absolute():
        raise SeedError("unsafe media root")
    cursor = root
    for component in components:
        cursor = cursor / component
        if cursor.is_symlink():
            raise SeedError("symlinked fixture path")
    try:
        cursor.relative_to(root)
    except ValueError as exc:
        raise SeedError("fixture path escaped media root") from exc
    return cursor


def _assert_real_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise SeedError(f"symlinked fixture directory: {path.name}")
    if path.exists() and not path.is_dir():
        raise SeedError(f"fixture path is not a directory: {path.name}")
    if not path.exists() and create:
        path.mkdir(parents=True, exist_ok=False)
    if not path.exists() or path.is_symlink() or not path.is_dir():
        raise SeedError(f"fixture directory is unavailable: {path.name}")


def _prepare_media_root(root: Path) -> None:
    if not root.is_absolute() or root.is_symlink() or not root.exists() or not root.is_dir():
        raise SeedError("MEDIA_STORE_ROOT must be an existing real directory")
    for name in ("public", "archive", ".staging", "e2e"):
        _assert_real_directory(_safe_child(root, name), create=True)


def _assert_media_areas_empty(root: Path) -> None:
    for name in ("public", "archive", ".staging", "e2e"):
        area = _safe_child(root, name)
        _assert_real_directory(area)
        try:
            children = list(area.iterdir())
        except OSError as exc:
            raise SeedError("cannot safely inspect the test media volume") from exc
        if children:
            raise SeedError(f"existing media prevents an atomic fixture seed: {name}")


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _target_specs() -> dict[str, dict[str, Any]]:
    return {
        "vip": {
            "id": _target_id("vip"),
            "name_zh": "VIP",
            "aliases": ["窗口", "中路窗口"],
            "description": f"{FIXTURE_NOTE}；固定 Target VIP。",
            "map_x": 36.87,
            "map_y": 44.29,
            "sort_order": 10,
        },
        "ct-spawn": {
            "id": _target_id("ct-spawn"),
            "name_zh": "警家",
            "aliases": [],
            "description": f"{FIXTURE_NOTE}；固定 Target 警家。",
            "map_x": 43.01,
            "map_y": 85.54,
            "sort_order": 20,
        },
        "bench": {
            "id": _target_id("bench"),
            "name_zh": "长椅",
            "aliases": [],
            "description": f"{FIXTURE_NOTE}；固定 Target 长椅且无本地路线。",
            "map_x": None,
            "map_y": None,
            "sort_order": 30,
        },
    }


def _origin_specs() -> dict[str, dict[str, Any]]:
    return {
        "t-spawn": {
            "id": _origin_id("t-spawn"),
            "name_zh": "T 出生点",
            "aliases": ["匪家"],
            "sort_order": 10,
        },
        "a-ramp": {
            "id": _origin_id("a-ramp"),
            "name_zh": "A1",
            "aliases": [],
            "sort_order": 20,
        },
    }


def _fixture_steps(label: str) -> tuple[str, str, str, str]:
    return tuple(
        f"{FIXTURE_NOTE}；测试目录 {label}：{step}。"
        for step in ("站位普通文字", "瞄点普通文字", "投掷普通文字", "结果普通文字")
    )


def _lineup_specs() -> tuple[LineupSpec, ...]:
    route_one = (
        f"{FIXTURE_NOTE}；测试路线一：站位普通文字。",
        f"{FIXTURE_NOTE}；测试路线一：瞄点普通文字。",
        f"{FIXTURE_NOTE}；测试路线一：原地跳投。",
        f"{FIXTURE_NOTE}；测试路线一：结果普通文字。",
    )
    route_two = (
        f"{FIXTURE_NOTE}；测试路线二：站位普通文字。",
        f"{FIXTURE_NOTE}；测试路线二：瞄点普通文字。",
        f"{FIXTURE_NOTE}；测试路线二：蹲着跳投。",
        f"{FIXTURE_NOTE}；测试路线二：结果普通文字。",
    )
    specs: list[LineupSpec] = [
        LineupSpec(0, "vip", "t-spawn", 10, True, *route_one),
        LineupSpec(1, "vip", "t-spawn", 20, False, *route_two),
        LineupSpec(2, "vip", "a-ramp", 10, True, *_fixture_steps("A1→VIP默认路线")),
        LineupSpec(3, "ct-spawn", "t-spawn", 10, True, *_fixture_steps("T出生点→警家默认路线")),
    ]
    for index in range(4, 20):
        label = f"合成测试目录{index - 3}"
        setup, aim, throw, result = _fixture_steps(label)
        specs.append(
            LineupSpec(
                index,
                "ct-spawn",
                "a-ramp",
                index - 3,
                index == 4,
                setup,
                aim,
                throw,
                result,
            )
        )
    return tuple(specs)


async def _snapshot() -> FixtureSnapshot:
    async with get_session_factory()() as session:
        return FixtureSnapshot(
            tuple((await session.execute(select(Target))).scalars().all()),
            tuple((await session.execute(select(TargetAlias))).scalars().all()),
            tuple((await session.execute(select(Origin))).scalars().all()),
            tuple((await session.execute(select(OriginAlias))).scalars().all()),
            tuple((await session.execute(select(Lineup))).scalars().all()),
            tuple((await session.execute(select(TutorialVideo))).scalars().all()),
        )


def _snapshot_empty(snapshot: FixtureSnapshot) -> bool:
    return not any(
        (
            snapshot.targets,
            snapshot.target_aliases,
            snapshot.origins,
            snapshot.origin_aliases,
            snapshot.lineups,
            snapshot.videos,
        )
    )


def _assert_aliases(
    rows: tuple[TargetAlias | OriginAlias, ...], expected: dict[uuid.UUID, list[str]]
) -> bool:
    actual: dict[uuid.UUID, list[str]] = {}
    for row in rows:
        owner_id = row.target_id if isinstance(row, TargetAlias) else row.origin_id
        actual.setdefault(owner_id, []).append(row.alias)
        if row.normalized_alias != normalize_text(row.alias):
            return False
    return {key: sorted(value) for key, value in actual.items()} == {
        key: sorted(value) for key, value in expected.items() if value
    }


def _file_is_regular(path: Path) -> bool:
    try:
        return path.exists() and not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _run_tool(arguments: list[str]) -> bytes:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SeedError(f"required media command unavailable: {arguments[0]}") from exc
    if completed.returncode != 0:
        raise SeedError(f"media command failed: {arguments[0]}")
    return completed.stdout


def _probe(path: Path) -> dict[str, Any]:
    payload = _run_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedError("ffprobe returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise SeedError("ffprobe returned an invalid payload")
    return value


def video_command(color: str, output: Path) -> list[str]:
    """Build the shell-free command for one 1-second solid-color MP4."""

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=1920x1080:r=30:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output),
    ]


def poster_command(video: Path, output: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:720",
        "-an",
        "-c:v",
        "libwebp",
        "-q:v",
        "85",
        str(output),
    ]


def wav_command(output: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000:duration=0.5",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]


def _assert_video_probe(payload: dict[str, Any]) -> int:
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise SeedError("generated video probe is incomplete")
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audios = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(videos) != 1 or len(audios) != 1:
        raise SeedError("generated video does not contain exactly one video and audio stream")
    video = videos[0]
    audio = audios[0]
    try:
        duration = float(video.get("duration") or format_data.get("duration"))
        width = int(video["width"])
        height = int(video["height"])
        channels = int(audio["channels"])
        sample_rate = int(audio["sample_rate"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SeedError("generated video metadata is invalid") from exc
    if (
        abs(duration - 1.0) > 0.05
        or (width, height) != (1920, 1080)
        or video.get("codec_name") != "h264"
        or video.get("pix_fmt") != "yuv420p"
        or video.get("avg_frame_rate") != "30/1"
        or video.get("r_frame_rate") != "30/1"
        or audio.get("codec_name") != "aac"
        or sample_rate != 48000
        or channels != 2
    ):
        raise SeedError("generated video does not satisfy H.264/yuv420p/30fps contract")
    return round(duration * 1000)


def _assert_poster_probe(payload: dict[str, Any]) -> None:
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise SeedError("generated poster probe is incomplete")
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    names = {part.strip() for part in str(format_data.get("format_name", "")).split(",")}
    if len(videos) != 1 or not names.intersection({"webp", "webp_pipe", "image2"}):
        raise SeedError("generated poster is not a WebP image")
    if videos[0].get("codec_name") != "webp" or (
        videos[0].get("width"),
        videos[0].get("height"),
    ) != (1280, 720):
        raise SeedError("generated poster dimensions are invalid")


def _assert_wav_probe(payload: dict[str, Any]) -> None:
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise SeedError("generated WAV probe is incomplete")
    audios = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    try:
        duration = float(format_data["duration"])
        sample_rate = int(audios[0]["sample_rate"])
        channels = int(audios[0]["channels"])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise SeedError("generated WAV metadata is invalid") from exc
    if (
        format_data.get("format_name") != "wav"
        or len(audios) != 1
        or audios[0].get("codec_name") != "pcm_s16le"
        or sample_rate != 16000
        or channels != 1
        or not 0 < duration <= 1
    ):
        raise SeedError("generated WAV does not satisfy the short recording contract")


def _generate_media(root: Path, specs: tuple[LineupSpec, ...]) -> tuple[list[MediaArtifact], Path]:
    staging = _safe_child(root, f".staging/seed-e2e-{uuid.uuid4().hex}")
    staging.mkdir(parents=False, exist_ok=False)
    artifacts: list[MediaArtifact] = []
    colors = [f"0x{(0x102030 + index * 0x071113) & 0xFFFFFF:06x}" for index in range(len(specs))]
    try:
        for spec, color in zip(specs, colors, strict=True):
            lineup_id = _lineup_id(spec.index)
            storage_key = uuid.uuid4()
            video_id = _video_id(spec.index)
            video = staging / f"video-{storage_key}.mp4"
            poster = staging / f"poster-{storage_key}.webp"
            _run_tool(video_command(color, video))
            _run_tool(poster_command(video, poster))
            if not _file_is_regular(video) or not _file_is_regular(poster):
                raise SeedError("FFmpeg did not produce regular fixture media")
            duration_ms = _assert_video_probe(_probe(video))
            _assert_poster_probe(_probe(poster))
            checksum = hashlib.sha256(video.read_bytes()).hexdigest()
            relative_path = f"{lineup_id}/primary-{storage_key}.mp4"
            poster_relative_path = f"{lineup_id}/poster-{storage_key}.webp"
            artifacts.append(
                MediaArtifact(
                    lineup_id,
                    video_id,
                    storage_key,
                    video,
                    poster,
                    relative_path,
                    poster_relative_path,
                    duration_ms,
                    video.stat().st_size,
                    checksum,
                )
            )
        wav = staging / "recording.wav"
        _run_tool(wav_command(wav))
        if not _file_is_regular(wav):
            raise SeedError("FFmpeg did not produce a regular WAV")
        _assert_wav_probe(_probe(wav))
        return artifacts, staging
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _matches_fixture_rows(snapshot: FixtureSnapshot) -> bool:
    target_specs = _target_specs()
    origin_specs = _origin_specs()
    lineup_specs = _lineup_specs()
    if {row.id for row in snapshot.targets} != {item["id"] for item in target_specs.values()}:
        return False
    if {row.id for row in snapshot.origins} != {item["id"] for item in origin_specs.values()}:
        return False
    if len(snapshot.lineups) != 20 or {row.id for row in snapshot.lineups} != {
        _lineup_id(i) for i in range(20)
    }:
        return False
    if len(snapshot.videos) != 20 or {row.id for row in snapshot.videos} != {
        _video_id(i) for i in range(20)
    }:
        return False
    targets_by_id = {row.id: row for row in snapshot.targets}
    origins_by_id = {row.id: row for row in snapshot.origins}
    for slug, spec in target_specs.items():
        row = targets_by_id[spec["id"]]
        if row.slug != slug or any(
            getattr(row, key) != value
            for key, value in spec.items()
            if key not in {"id", "aliases", "map_x", "map_y"}
        ):
            return False
        if (
            row.name_en is not None
            or not row.is_active
            or (None if row.map_x is None else float(row.map_x)) != spec["map_x"]
            or (None if row.map_y is None else float(row.map_y)) != spec["map_y"]
        ):
            return False
    for slug, spec in origin_specs.items():
        row = origins_by_id[spec["id"]]
        if (
            row.slug != slug
            or row.name_zh != spec["name_zh"]
            or row.sort_order != spec["sort_order"]
            or not row.is_active
        ):
            return False
    if not _assert_aliases(
        tuple(snapshot.target_aliases),
        {spec["id"]: spec["aliases"] for spec in target_specs.values()},
    ):
        return False
    if not _assert_aliases(
        tuple(snapshot.origin_aliases),
        {spec["id"]: spec["aliases"] for spec in origin_specs.values()},
    ):
        return False
    lineups_by_id = {row.id: row for row in snapshot.lineups}
    videos_by_lineup = {row.lineup_id: row for row in snapshot.videos}
    if len(videos_by_lineup) != 20:
        return False
    for spec in lineup_specs:
        row = lineups_by_id[_lineup_id(spec.index)]
        if (
            row.target_id != target_specs[spec.target_slug]["id"]
            or row.origin_id != origin_specs[spec.origin_slug]["id"]
            or row.sort_order != spec.sort_order
            or row.is_default != spec.is_default
            or _status_value(row.status) != LineupStatus.published.value
            or row.default_needs_review
            or (row.setup_text, row.aim_text, row.throw_text, row.result_text)
            != (spec.setup, spec.aim, spec.throw, spec.result)
            or row.last_checked_at is not None
            or row.game_version_note is not None
        ):
            return False
        video = videos_by_lineup.get(row.id)
        if (
            video is None
            or _status_value(video.status) != VideoStatus.ready.value
            or video.rights_note != FIXTURE_NOTE
            or video.failure_reason is not None
        ):
            return False
        if (
            not video.storage_key
            or not video.relative_path
            or not video.poster_relative_path
            or not video.duration_ms
            or not video.size_bytes
            or not video.checksum_sha256
        ):
            return False
    return True


def _verify_complete_media(root: Path, snapshot: FixtureSnapshot) -> bool:
    expected: set[str] = set()
    for video in snapshot.videos:
        if not video.relative_path or not video.poster_relative_path or not video.storage_key:
            return False
        if (
            video.relative_path != f"{video.lineup_id}/primary-{video.storage_key}.mp4"
            or video.poster_relative_path != f"{video.lineup_id}/poster-{video.storage_key}.webp"
        ):
            return False
        for relative in (f"public/{video.relative_path}", f"public/{video.poster_relative_path}"):
            path = _safe_child(root, relative)
            if not _file_is_regular(path):
                return False
            expected.add(relative)
        video_path = _safe_child(root, f"public/{video.relative_path}")
        if (
            video.size_bytes != video_path.stat().st_size
            or hashlib.sha256(video_path.read_bytes()).hexdigest() != video.checksum_sha256
        ):
            return False
        try:
            measured_duration = _assert_video_probe(_probe(video_path))
            _assert_poster_probe(_probe(_safe_child(root, f"public/{video.poster_relative_path}")))
        except SeedError:
            return False
        if video.duration_ms != measured_duration:
            return False
    wav = _safe_child(root, WAV_RELATIVE_PATH)
    if not _file_is_regular(wav):
        return False
    try:
        _assert_wav_probe(_probe(wav))
    except SeedError:
        return False
    actual: set[str] = set()
    public = _safe_child(root, "public")
    for path in public.rglob("*"):
        if path.is_symlink() or path.is_file():
            if path.is_symlink() or not path.is_file():
                return False
            actual.add(path.relative_to(root).as_posix())
    return actual == expected


def _move_files(
    root: Path, artifacts: list[MediaArtifact], staging: Path
) -> list[tuple[Path, Path]]:
    journal: list[tuple[Path, Path]] = []
    sources = [
        item for artifact in artifacts for item in (artifact.staged_video, artifact.staged_poster)
    ] + [staging / "recording.wav"]
    destinations = [
        _safe_child(root, f"public/{relative}")
        for artifact in artifacts
        for relative in (artifact.relative_path, artifact.poster_relative_path)
    ] + [_safe_child(root, WAV_RELATIVE_PATH)]
    try:
        for source, destination in zip(sources, destinations, strict=True):
            if not _file_is_regular(source) or destination.exists() or destination.is_symlink():
                raise SeedError("fixture media move path is occupied or unsafe")
            _assert_real_directory(destination.parent, create=True)
            try:
                os.replace(source, destination)
            except OSError as exc:
                raise SeedError("fixture media publication failed") from exc
            journal.append((source, destination))
    except BaseException:
        _rollback_moves(journal)
        raise
    return journal


def _rollback_moves(journal: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(journal):
        if not destination.exists():
            continue
        if source.exists() or source.is_symlink():
            destination.unlink(missing_ok=True)
        else:
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)


async def seed(settings: Settings, environ: Mapping[str, str] | None = None) -> str:
    """Validate, create, or report the fixed fixture without partial DB/file state."""

    validate_seed_environment(settings, environ)
    root = Path(settings.media_store_root)
    _prepare_media_root(root)
    snapshot = await _snapshot()
    if _matches_fixture_rows(snapshot):
        if not _verify_complete_media(root, snapshot):
            raise SeedError("fixture database is complete but media is missing or invalid")
        return "E2E_SEED_EXISTING lineups=20 videos=20 wav=1"
    if not _snapshot_empty(snapshot):
        raise SeedError("existing partial, extra, or non-conforming fixture rows were found")
    _assert_media_areas_empty(root)
    specs = _lineup_specs()
    artifacts, staging = _generate_media(root, specs)
    journal: list[tuple[Path, Path]] = []
    try:
        async with media_lock(timeout_seconds=5):
            if not _snapshot_empty(await _snapshot()):
                raise SeedError("fixture state changed before the atomic seed")
            target_specs = _target_specs()
            origin_specs = _origin_specs()
            async with get_session_factory()() as session:
                async with session.begin():
                    targets = [
                        Target(
                            id=spec["id"],
                            slug=slug,
                            name_zh=spec["name_zh"],
                            name_en=None,
                            description=spec["description"],
                            map_x=spec["map_x"],
                            map_y=spec["map_y"],
                            sort_order=spec["sort_order"],
                            is_active=True,
                        )
                        for slug, spec in target_specs.items()
                    ]
                    origins = [
                        Origin(
                            id=spec["id"],
                            slug=slug,
                            name_zh=spec["name_zh"],
                            name_en=None,
                            description=f"{FIXTURE_NOTE}；固定 Origin {spec['name_zh']}。",
                            map_x=None,
                            map_y=None,
                            sort_order=spec["sort_order"],
                            is_active=True,
                        )
                        for slug, spec in origin_specs.items()
                    ]
                    session.add_all(targets + origins)
                    # These models intentionally expose only scalar foreign-key ids.
                    # Flush parents explicitly instead of relying on relationship-based
                    # unit-of-work ordering before aliases and lineups are inserted.
                    await session.flush()
                    session.add_all(
                        [
                            TargetAlias(
                                id=_fixture_id("target-alias", f"{slug}:{alias}"),
                                target_id=spec["id"],
                                alias=alias,
                                normalized_alias=normalize_text(alias),
                            )
                            for slug, spec in target_specs.items()
                            for alias in spec["aliases"]
                        ]
                        + [
                            OriginAlias(
                                id=_fixture_id("origin-alias", f"{slug}:{alias}"),
                                origin_id=spec["id"],
                                alias=alias,
                                normalized_alias=normalize_text(alias),
                            )
                            for slug, spec in origin_specs.items()
                            for alias in spec["aliases"]
                        ]
                    )
                    lineups: list[Lineup] = []
                    for spec in specs:
                        lineups.append(
                            Lineup(
                                id=_lineup_id(spec.index),
                                target_id=target_specs[spec.target_slug]["id"],
                                origin_id=origin_specs[spec.origin_slug]["id"],
                                setup_text=spec.setup,
                                aim_text=spec.aim,
                                throw_text=spec.throw,
                                result_text=spec.result,
                                is_default=spec.is_default,
                                default_needs_review=False,
                                sort_order=spec.sort_order,
                                status=LineupStatus.published,
                                last_checked_at=None,
                                game_version_note=None,
                            )
                        )
                    session.add_all(lineups)
                    # TutorialVideo has the same scalar-id relationship to Lineup.
                    # Keep every flush inside this transaction, but make the FK order
                    # deterministic before publishing files and inserting video rows.
                    await session.flush()
                    journal = _move_files(root, artifacts, staging)
                    session.add_all(
                        [
                            TutorialVideo(
                                id=artifact.video_id,
                                lineup_id=artifact.lineup_id,
                                status=VideoStatus.ready,
                                storage_key=artifact.storage_key,
                                relative_path=artifact.relative_path,
                                poster_relative_path=artifact.poster_relative_path,
                                duration_ms=artifact.duration_ms,
                                size_bytes=artifact.size_bytes,
                                checksum_sha256=artifact.checksum_sha256,
                                rights_note=FIXTURE_NOTE,
                                failure_reason=None,
                            )
                            for artifact in artifacts
                        ]
                    )
                    await session.flush()
    except BaseException:
        try:
            _rollback_moves(journal)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return "E2E_SEED_CREATED lineups=20 videos=20 wav=1"


def main() -> int:
    try:
        settings = get_settings()
        result = asyncio.run(seed(settings))
    except (SeedGuardError, SeedError) as exc:
        print(f"E2E_SEED_REJECTED: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("E2E_SEED_REJECTED: seed failed", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
