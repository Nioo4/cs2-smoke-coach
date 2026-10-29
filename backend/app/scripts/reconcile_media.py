"""Idempotent startup media reconciliation (never exposed as an HTTP route)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from app.core.config import get_settings
from app.db.models import Lineup, LineupStatus, TutorialVideo, VideoStatus
from app.db.session import get_session_factory
from app.services.media import (
    MAX_OUTPUT_BYTES,
    MediaError,
    MediaLockTimeout,
    _output_probe_is_fixed,
    _poster_dimensions_match_video,
    _probe,
    _safe_relative_path,
    media_lock,
)
from app.services.media import (
    _poster_probe_is_valid as _media_poster_probe_is_valid,
)

logger = logging.getLogger("smokeline")


class MediaReconcileError(RuntimeError):
    """A filesystem item or database state cannot be safely reconciled."""


@dataclass(frozen=True, slots=True)
class _ComponentCheck:
    path: Path | None
    present: bool
    valid: bool
    invalid: bool = False
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _VideoCheck:
    healthy: bool
    any_present: bool
    any_invalid: bool = False


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value))


async def _content_lock(session) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '5s'"))
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1397575499})


def _safe_media_path(root: Path, relative: str | None) -> Path | None:
    """Resolve a DB relative path without following any path component."""

    if relative is None:
        return None
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise MediaReconcileError("unsafe media path")
    if root.is_symlink():
        raise MediaReconcileError("unsafe media root")
    path = _safe_relative_path(root, relative)
    if path is None:
        raise MediaReconcileError("unsafe media path")
    cursor = root
    for component in relative.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            raise MediaReconcileError("unsafe media path")
        cursor = cursor / component
        try:
            if cursor.is_symlink():
                raise MediaReconcileError("symlinked media path")
        except OSError as exc:
            raise MediaReconcileError("media path inspection failed") from exc
    return path


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MediaReconcileError("media checksum failed") from exc
    return digest.hexdigest()


def _probe_duration(payload: dict[str, Any]) -> float:
    streams = payload.get("streams")
    format_data = payload.get("format")
    candidates: list[Any] = []
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                candidates.append(stream.get("duration"))
    if isinstance(format_data, dict):
        candidates.append(format_data.get("duration"))
    for candidate in candidates:
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    return 0.0


def _fps(payload: dict[str, Any]) -> float:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return 0.0
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        value = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0")
        try:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    return 0.0


def _video_probe_is_valid(payload: dict[str, Any], video: TutorialVideo) -> bool:
    if not _output_probe_is_fixed(payload):
        return False
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return False
    video_streams = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(video_streams) != 1:
        return False
    format_data = payload.get("format")
    if not isinstance(format_data, dict):
        return False
    format_names = {
        name.strip().casefold()
        for name in str(format_data.get("format_name", "")).split(",")
        if name.strip()
    }
    if not format_names.intersection({"mov", "mp4"}):
        return False
    duration = _probe_duration(payload)
    if not 0 < duration <= 20.0 + 1e-6:
        return False
    if not 29.5 <= _fps(payload) <= 30.5:
        return False
    expected_ms = getattr(video, "duration_ms", None)
    if expected_ms is None or abs(duration * 1000 - expected_ms) > max(250, expected_ms * 0.05):
        return False
    return True


def _poster_probe_is_valid(
    payload: dict[str, Any], video_payload: dict[str, Any] | None = None
) -> bool:
    if not _media_poster_probe_is_valid(payload):
        return False
    return video_payload is None or _poster_dimensions_match_video(payload, video_payload)


async def _inspect_component(
    root: Path,
    relative: str | None,
    *,
    kind: str,
    video: TutorialVideo,
    video_payload: dict[str, Any] | None = None,
) -> _ComponentCheck:
    path = _safe_media_path(root, relative)
    if path is None:
        return _ComponentCheck(None, False, False, True)
    try:
        exists = path.exists()
    except OSError as exc:
        raise MediaReconcileError("media existence check failed") from exc
    if not exists:
        return _ComponentCheck(path, False, False, False)
    try:
        if path.is_symlink():
            raise MediaReconcileError("symlinked media file")
        if not path.is_file():
            return _ComponentCheck(path, True, False, True)
        size = path.stat().st_size
    except OSError as exc:
        raise MediaReconcileError("media stat failed") from exc
    if size <= 0 or size > MAX_OUTPUT_BYTES:
        return _ComponentCheck(path, True, False, True)
    try:
        if kind == "video":
            expected_size = getattr(video, "size_bytes", None)
            expected_checksum = getattr(video, "checksum_sha256", None)
            if expected_size != size or not expected_checksum:
                return _ComponentCheck(path, True, False, True)
            if _checksum(path) != expected_checksum:
                return _ComponentCheck(path, True, False, True)
            payload = await _probe(path)
            valid = _video_probe_is_valid(payload, video)
            return _ComponentCheck(path, True, valid, not valid, payload if valid else None)
        else:
            payload = await _probe(path)
            valid = _poster_probe_is_valid(payload, video_payload)
    except subprocess.CalledProcessError:
        valid = False
    except MediaError:
        valid = False
    return _ComponentCheck(path, True, valid, not valid)


def _trash_destination(root: Path, source_root: str, relative: str) -> Path:
    base = root / "trash" / date.today().isoformat() / source_root
    destination = _safe_media_path(base, relative)
    if destination is None:
        raise MediaReconcileError("unsafe trash path")
    if destination.exists():
        destination = destination.with_name(
            f"{destination.stem}.reconcile-{uuid.uuid4().hex}{destination.suffix}"
        )
    return destination


def _move_to_trash(
    root: Path, source_root: str, relative: str | None, journal: list[tuple[Path, Path]]
) -> None:
    if relative is None:
        return
    source = _safe_media_path(root / source_root, relative)
    if source is None:
        return
    try:
        exists = source.exists()
    except OSError as exc:
        raise MediaReconcileError("media existence check failed") from exc
    if not exists:
        return
    if source.is_symlink():
        raise MediaReconcileError("symlinked media file")
    if not source.is_file():
        raise MediaReconcileError("non-regular media file")
    destination = _trash_destination(root, source_root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise MediaReconcileError("media trash move failed") from exc
    journal.append((source, destination))


def _move_to_expected(
    root: Path,
    source_root: str,
    destination_root: str,
    relative: str,
    journal: list[tuple[Path, Path]],
) -> None:
    source = _safe_media_path(root / source_root, relative)
    destination = _safe_media_path(root / destination_root, relative)
    if source is None or destination is None:
        raise MediaReconcileError("unsafe media path")
    if not source.exists() or source.is_symlink() or not source.is_file():
        raise MediaReconcileError("media source disappeared")
    if destination.exists():
        raise MediaReconcileError("media destination occupied")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise MediaReconcileError("media position repair failed") from exc
    journal.append((source, destination))


async def _reconcile_component(
    root: Path,
    relative: str | None,
    *,
    kind: str,
    video: TutorialVideo,
    video_payload: dict[str, Any] | None = None,
    expected_root: str,
    other_root: str,
    journal: list[tuple[Path, Path]],
) -> _ComponentCheck:
    expected = await _inspect_component(
        root / expected_root,
        relative,
        kind=kind,
        video=video,
        video_payload=video_payload,
    )
    other = await _inspect_component(
        root / other_root,
        relative,
        kind=kind,
        video=video,
        video_payload=video_payload,
    )
    if expected.valid:
        if other.present:
            _move_to_trash(root, other_root, relative, journal)
        return expected
    if other.valid:
        if expected.present:
            _move_to_trash(root, expected_root, relative, journal)
        if relative is None:
            raise MediaReconcileError("missing media path")
        _move_to_expected(root, other_root, expected_root, relative, journal)
        return _ComponentCheck(
            _safe_media_path(root / expected_root, relative),
            True,
            True,
            payload=other.payload,
        )
    if expected.present:
        _move_to_trash(root, expected_root, relative, journal)
    if other.present:
        _move_to_trash(root, other_root, relative, journal)
    return _ComponentCheck(
        expected.path,
        expected.present or other.present,
        False,
        expected.invalid or other.invalid,
    )


async def _reconcile_video(
    root: Path, video: TutorialVideo, journal: list[tuple[Path, Path]]
) -> _VideoCheck:
    status = _status_value(video.status)
    if status not in {VideoStatus.ready.value, VideoStatus.archived.value}:
        return _VideoCheck(True, False)
    expected_root, other_root = (
        ("archive", "public") if status == VideoStatus.archived.value else ("public", "archive")
    )
    video_component = await _reconcile_component(
        root,
        video.relative_path,
        kind="video",
        video=video,
        expected_root=expected_root,
        other_root=other_root,
        journal=journal,
    )
    poster_component = await _reconcile_component(
        root,
        video.poster_relative_path,
        kind="poster",
        video=video,
        video_payload=video_component.payload,
        expected_root=expected_root,
        other_root=other_root,
        journal=journal,
    )
    healthy = video_component.valid and poster_component.valid
    if not healthy:
        # A valid half-pair is still unreferenced once the DB row is cleared.
        # Move it with the damaged half before the short pointer transaction.
        _move_to_trash(root, expected_root, video.relative_path, journal)
        _move_to_trash(root, expected_root, video.poster_relative_path, journal)
    return _VideoCheck(
        healthy,
        video_component.present or poster_component.present,
        video_component.invalid or poster_component.invalid,
    )


def _video_identity(video: TutorialVideo) -> tuple[Any, ...]:
    return (
        _status_value(video.status),
        video.storage_key,
        video.relative_path,
        video.poster_relative_path,
        video.duration_ms,
        video.size_bytes,
        video.checksum_sha256,
        video.lineup_id,
    )


def _rollback_moves(journal: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(journal):
        if not destination.exists():
            continue
        if source.exists():
            raise MediaReconcileError("media rollback destination occupied")
        source.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(destination, source)
        except OSError as exc:
            raise MediaReconcileError("media rollback failed") from exc


async def _read_snapshot() -> tuple[list[TutorialVideo], list[Lineup]]:
    async with get_session_factory()() as session:
        # The session context rolls back/closes the read transaction.  An
        # explicit rollback here would expire the ORM instances before they
        # are returned, making the detached snapshot unusable by reconcile.
        videos = list((await session.execute(select(TutorialVideo))).scalars().all())
        lineups = list((await session.execute(select(Lineup))).scalars().all())
        return videos, lineups


def _candidate_is_healthy(
    lineup: Lineup,
    current_by_lineup: dict[uuid.UUID, TutorialVideo],
    snapshot_videos: list[TutorialVideo],
    checks: dict[uuid.UUID, _VideoCheck],
) -> bool:
    current_video = current_by_lineup.get(lineup.id)
    snapshot_video = next(
        (video for video in snapshot_videos if video.lineup_id == lineup.id), None
    )
    return (
        current_video is not None
        and snapshot_video is not None
        and checks.get(current_video.id, _VideoCheck(False, False)).healthy
        and _video_identity(current_video) == _video_identity(snapshot_video)
    )


async def _apply_failures(
    videos: list[TutorialVideo],
    lineups: list[Lineup],
    checks: dict[uuid.UUID, _VideoCheck],
    bad_lineups: set[uuid.UUID],
) -> None:
    invalid_ids = {
        video.id
        for video in videos
        if video.id in checks
        and not checks[video.id].healthy
        and _status_value(video.status) in {VideoStatus.ready.value, VideoStatus.archived.value}
    }
    if not invalid_ids and not bad_lineups:
        return
    snapshot_videos = {video.id: video for video in videos}
    groups: dict[tuple[uuid.UUID, uuid.UUID], list[Lineup]] = {}
    for lineup in lineups:
        groups.setdefault((lineup.target_id, lineup.origin_id), []).append(lineup)
    replacement_groups: list[tuple[uuid.UUID, uuid.UUID]] = []
    async with get_session_factory()() as session:
        async with session.begin():
            await _content_lock(session)
            for video_id in invalid_ids:
                current = await session.get(TutorialVideo, video_id, with_for_update=True)
                original = snapshot_videos[video_id]
                if current is None:
                    continue
                if _video_identity(current) != _video_identity(original):
                    raise MediaReconcileError("video changed during reconciliation")
                current.status = VideoStatus.failed
                current.storage_key = None
                current.relative_path = None
                current.poster_relative_path = None
                current.duration_ms = None
                current.size_bytes = None
                current.checksum_sha256 = None
                current.failure_reason = (
                    "media_corrupt" if checks[video_id].any_invalid else "media_missing"
                )

            current_bad_lineups: set[uuid.UUID] = set()
            for lineup_id in bad_lineups:
                current_lineup = await session.get(Lineup, lineup_id, with_for_update=True)
                if (
                    current_lineup is not None
                    and _status_value(current_lineup.status) == LineupStatus.published.value
                ):
                    current_bad_lineups.add(lineup_id)

            current_videos_result = await session.execute(select(TutorialVideo))
            current_videos = list(current_videos_result.scalars().all())
            current_by_lineup: dict[uuid.UUID, TutorialVideo] = {
                video.lineup_id: video for video in current_videos
            }
            for group_key, snapshot_group in groups.items():
                original_defaults = [
                    lineup
                    for lineup in snapshot_group
                    if _status_value(lineup.status) == LineupStatus.published.value
                    and lineup.is_default
                ]
                if not original_defaults:
                    continue
                current_group_result = await session.execute(
                    select(Lineup)
                    .where(Lineup.target_id == group_key[0], Lineup.origin_id == group_key[1])
                    .with_for_update()
                )
                current_group = list(current_group_result.scalars().all())
                current_default = next(
                    (
                        lineup
                        for lineup in current_group
                        if _status_value(lineup.status) == LineupStatus.published.value
                        and lineup.is_default
                    ),
                    None,
                )
                if current_default is None or current_default.id not in current_bad_lineups:
                    continue
                for lineup in current_group:
                    if lineup.is_default or lineup.default_needs_review:
                        lineup.is_default = False
                        lineup.default_needs_review = False
                # The partial unique index must see the old default cleared
                # before the replacement is flushed.
                await session.flush()
                candidates = [
                    lineup
                    for lineup in current_group
                    if _status_value(lineup.status) == LineupStatus.published.value
                    and lineup.id not in current_bad_lineups
                    and _candidate_is_healthy(
                        lineup, current_by_lineup, list(snapshot_videos.values()), checks
                    )
                ]
                if not candidates:
                    continue
                candidate = min(
                    candidates,
                    key=lambda lineup: (
                        lineup.sort_order,
                        str(lineup.id),
                    ),
                )
                candidate.is_default = True
                candidate.default_needs_review = True
                replacement_groups.append(group_key)

            for lineup_id in current_bad_lineups:
                current_lineup = await session.get(Lineup, lineup_id, with_for_update=True)
                if current_lineup is None:
                    continue
                current_lineup.status = LineupStatus.needs_revalidation
                current_lineup.is_default = False
                current_lineup.default_needs_review = False
                current_lineup.last_checked_at = None
    for target_id, origin_id in replacement_groups:
        logger.warning(
            "media default replaced",
            extra={
                "event": "MEDIA_DEFAULT_REPLACED",
                "error_code": "MEDIA_DEFAULT_REPLACED",
                "target_id": str(target_id),
                "origin_id": str(origin_id),
            },
        )


async def reconcile_media() -> None:
    """Repair media positions and hide/replace invalid published routes.

    The media lock spans the filesystem snapshot, ffprobe/checksum work, file
    moves, and each short PostgreSQL pointer transaction.  No FFmpeg runs here.
    """

    settings = get_settings()
    root = Path(settings.media_store_root)
    for directory in (root, root / "public", root / "archive", root / "trash"):
        directory.mkdir(parents=True, exist_ok=True)
    # The caller owns the shared media lock.  Keeping this function lock-free
    # at its boundary makes the lock order explicit: media lock first, then
    # the short PostgreSQL content-lock transaction in _apply_failures().
    videos, lineups = await _read_snapshot()
    journal: list[tuple[Path, Path]] = []
    try:
        checks: dict[uuid.UUID, _VideoCheck] = {}
        for video in videos:
            checks[video.id] = await _reconcile_video(root, video, journal)
        by_lineup: dict[uuid.UUID, list[TutorialVideo]] = {}
        for video in videos:
            by_lineup.setdefault(video.lineup_id, []).append(video)
        bad_lineups: set[uuid.UUID] = set()
        for lineup in lineups:
            if _status_value(lineup.status) != LineupStatus.published.value:
                continue
            route_videos = by_lineup.get(lineup.id, [])
            healthy = any(
                _status_value(video.status) == VideoStatus.ready.value
                and checks.get(video.id, _VideoCheck(False, False)).healthy
                for video in route_videos
            )
            if not healthy:
                bad_lineups.add(lineup.id)
        await _apply_failures(videos, lineups, checks, bad_lineups)
    except Exception:
        _rollback_moves(journal)
        raise


async def _run_reconcile_with_lock() -> None:
    async with media_lock(timeout_seconds=120):
        await reconcile_media()


def main() -> int:
    try:
        asyncio.run(_run_reconcile_with_lock())
    except (MediaLockTimeout, MediaReconcileError):
        logger.error(
            "media reconciliation failed",
            extra={"event": "MEDIA_RECONCILE_FAILED", "error_code": "MEDIA_RECONCILE_FAILED"},
        )
        return 1
    except Exception:
        logger.error(
            "media reconciliation failed",
            extra={"event": "MEDIA_RECONCILE_FAILED", "error_code": "MEDIA_RECONCILE_FAILED"},
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
