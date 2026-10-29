"""Streaming video validation, transcoding, and atomic media publication."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.errors import database_is_unavailable
from app.db.models import TutorialVideo, VideoStatus

MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 12 * 1024 * 1024
MAX_OUTPUT_WIDTH = 1920
MAX_OUTPUT_HEIGHT = 1080
MAX_POSTER_WIDTH = 1280
MAX_DURATION_SECONDS = 20.0
MAX_WIDTH = 3840
MAX_HEIGHT = 2160
MAX_FPS = 120.0
PROCESSING_TIMEOUT_SECONDS = 120.0
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime"}
_TRANSCODE_SLOT = asyncio.Semaphore(1)


class MediaError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        failure_reason: str | None = None,
        *,
        persist_failure: bool = False,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.failure_reason = failure_reason
        self.persist_failure = persist_failure
        super().__init__(message)


class MediaBusy(MediaError):
    def __init__(self) -> None:
        super().__init__(409, "MEDIA_BUSY", "媒体处理繁忙，请稍后重试。")


class MediaCommitUnknown(MediaError):
    """The DB outcome is unknown; retain files for startup reconciliation."""

    def __init__(
        self,
        status_code: int = 500,
        code: str = "INTERNAL_ERROR",
        message: str = "服务暂时异常，请凭请求编号重试。",
    ) -> None:
        super().__init__(status_code, code, message)


class MediaLockTimeout(TimeoutError):
    """The shared media lock was not acquired within its caller's budget."""


@dataclass(frozen=True, slots=True)
class ProcessedMedia:
    storage_key: uuid.UUID
    relative_path: str
    poster_relative_path: str
    duration_ms: int
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class Probe:
    format_names: frozenset[str]
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def _prepare_safe_directory(path: Path, *, create: bool) -> bool:
    """Check each directory component without resolving or following links."""

    try:
        absolute = path.absolute()
        cursor = Path(absolute.anchor)
        components = absolute.parts[1:] if absolute.anchor else absolute.parts
        for component in components:
            cursor /= component
            if cursor.is_symlink():
                return False
            if cursor.exists():
                if not cursor.is_dir():
                    return False
            elif create:
                cursor.mkdir()
        return absolute.is_dir() and not absolute.is_symlink()
    except OSError:
        return False


class _MediaLock:
    """A process and host-wide lock backed by the prescribed lock file."""

    def __init__(self, path: Path, timeout_seconds: float | None = None) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle = None
        self._acquired = False

    async def __aenter__(self):
        try:
            # Lock operations are explicitly non-blocking.  Keeping them on
            # the event-loop thread makes cancellation atomic with handle
            # cleanup; there is no worker that can acquire after cancellation.
            self._open()
            deadline = (
                None
                if self.timeout_seconds is None
                else time.monotonic() + max(0.0, self.timeout_seconds)
            )
            while True:
                if self._try_acquire():
                    return self
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaLockTimeout("media lock timeout")
                    await asyncio.sleep(min(0.05, remaining))
                else:
                    await asyncio.sleep(0.05)
        except BaseException:
            # _try_acquire is non-blocking, so cancellation cannot leave a
            # worker thread waiting to acquire this handle after __aenter__
            # has returned.  Closing also releases a race-winning OS lock.
            self._close()
            raise

    async def __aexit__(self, *_args):
        self._release()

    def _open(self) -> None:
        if not _prepare_safe_directory(self.path.parent, create=True) or self.path.is_symlink():
            raise OSError(errno.ELOOP, "unsafe media lock path")
        self._handle = self.path.open("a+b")

    def _try_acquire(self) -> bool:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                # The deployable runtime is Linux.  This branch keeps local
                # Windows unit tests deterministic without adding a dependency.
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        self._acquired = True
        return True

    def _close(self) -> None:
        if self._handle is None:
            return
        try:
            if self._acquired:
                self._release()
            else:
                self._handle.close()
                self._handle = None
        except OSError:
            self._handle = None

    def _release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None
            self._acquired = False


def media_root() -> Path:
    return Path(get_settings().media_store_root)


def media_lock(timeout_seconds: float | None = None) -> _MediaLock:
    return _MediaLock(media_root() / ".media.lock", timeout_seconds)


def _ensure_not_symlink(path: Path) -> bool:
    return path.exists() and not path.is_symlink() and path.is_file()


def _safe_relative_path(root: Path, relative: str | None) -> Path | None:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return None
    if relative.startswith("/") or any(
        component in {"", ".", ".."} for component in relative.split("/")
    ):
        return None
    if root.is_symlink():
        return None
    path = root.joinpath(*relative.split("/"))
    cursor = root
    try:
        for component in relative.split("/"):
            cursor /= component
            if cursor.is_symlink():
                return None
    except OSError:
        return None
    return path


def public_artifacts_look_valid(video: TutorialVideo) -> bool:
    """Cheap DB-side visibility check; expensive probing happens on upload/reconcile."""

    if video.status != VideoStatus.ready:
        return False
    root = media_root() / "public"
    video_path = _safe_relative_path(root, video.relative_path)
    poster_path = _safe_relative_path(root, video.poster_relative_path)
    if video_path is None or poster_path is None:
        return False
    if not _ensure_not_symlink(video_path) or not _ensure_not_symlink(poster_path):
        return False
    if video.size_bytes is None or not 1 <= video.size_bytes <= MAX_OUTPUT_BYTES:
        return False
    if video_path.stat().st_size != video.size_bytes:
        return False
    if video.duration_ms is None or not 1 <= video.duration_ms <= 20_000:
        return False
    if not video.checksum_sha256:
        return False
    digest = hashlib.sha256(video_path.read_bytes()).hexdigest()
    return digest == video.checksum_sha256


def _move_pair_between_roots(
    paths: tuple[str | None, str | None], source_root: str, destination_root: str
) -> None:
    """Move one complete media pair while the caller owns the media lock."""

    root = media_root()
    sources: list[Path] = []
    destinations: list[Path] = []
    for relative in paths:
        if not relative:
            raise OSError(errno.EINVAL, "incomplete media pair")
        source = _safe_relative_path(root / source_root, relative)
        destination = _safe_relative_path(root / destination_root, relative)
        if source is None or destination is None:
            raise OSError(errno.ELOOP, "unsafe media path")
        if not _ensure_not_symlink(source):
            raise OSError(errno.ENOENT, "media artifact missing")
        if destination.exists() or destination.is_symlink():
            raise OSError(errno.EEXIST, "media destination occupied")
        if not _prepare_safe_directory(destination.parent, create=True):
            raise OSError(errno.ELOOP, "unsafe media directory")
        sources.append(source)
        destinations.append(destination)

    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in zip(sources, destinations, strict=True):
            os.replace(source, destination)
            moved.append((source, destination))
        _fsync_directory(sources[0].parent)
        _fsync_directory(destinations[0].parent)
    except BaseException:
        for source, destination in reversed(moved):
            try:
                if destination.exists() and not destination.is_symlink() and not source.exists():
                    os.replace(destination, source)
            except OSError:
                pass
        if moved:
            _fsync_directory(sources[0].parent)
            _fsync_directory(destinations[0].parent)
        raise


async def move_public_pair_to_archive(
    paths: tuple[str | None, str | None], *, assume_locked: bool = False
) -> None:
    """Move a ready public pair into its same-relative-path archive location."""

    if assume_locked:
        _move_pair_between_roots(paths, "public", "archive")
        return
    async with media_lock(timeout_seconds=5):
        _move_pair_between_roots(paths, "public", "archive")


async def move_archived_pair_to_public(
    paths: tuple[str | None, str | None], *, assume_locked: bool = False
) -> None:
    """Move a validated archived pair back to its public location."""

    if assume_locked:
        _move_pair_between_roots(paths, "archive", "public")
        return
    async with media_lock(timeout_seconds=5):
        _move_pair_between_roots(paths, "archive", "public")


async def archived_artifacts_look_valid(video: TutorialVideo) -> bool:
    """Probe a stored archive pair before allowing it back into public."""

    root = media_root() / "archive"
    video_path = _safe_relative_path(root, video.relative_path)
    poster_path = _safe_relative_path(root, video.poster_relative_path)
    if video_path is None or poster_path is None:
        return False
    if not _ensure_not_symlink(video_path) or not _ensure_not_symlink(poster_path):
        return False
    if video.size_bytes is None or video.duration_ms is None or not video.checksum_sha256:
        return False
    try:
        if video_path.stat().st_size != video.size_bytes:
            return False
        if hashlib.sha256(video_path.read_bytes()).hexdigest() != video.checksum_sha256:
            return False
        output_probe = await _probe(video_path)
        poster_probe = await _probe(poster_path)
    except (OSError, TimeoutError, subprocess.CalledProcessError, MediaError):
        return False
    return (
        _output_duration_ms(output_probe) == video.duration_ms
        and _output_probe_is_fixed(output_probe)
        and _poster_probe_is_valid(poster_probe)
        and _poster_dimensions_match_video(poster_probe, output_probe)
    )


async def _run_process(args: list[str], timeout: float) -> tuple[bytes, bytes]:
    """Run an executable without a shell and kill it on timeout/cancellation."""

    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        drain_task = asyncio.create_task(process.communicate())
        await _await_task_settled(drain_task)
        raise
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
    return stdout, stderr


async def _await_task_settled(task: asyncio.Task[Any]) -> tuple[Any, bool]:
    """Wait for a shielded task even when its caller is cancelled.

    The cancellation is reported to the caller after the operation has a
    definite result.  This is used for both subprocess draining and the DB
    commit callback so a request cannot release a resource while work is
    still running or roll back files after an already-committed pointer.
    """

    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _probe(path: Path, timeout: float = 15.0) -> dict[str, Any]:
    stdout, _stderr = await _run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout,
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaError(422, "INVALID_VIDEO", "视频无法解析。", "processing_failed") from exc
    if not isinstance(payload, dict):
        raise MediaError(422, "INVALID_VIDEO", "视频无法解析。", "processing_failed")
    return payload


def _fraction(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _validate_probe(payload: dict[str, Any], mime: str) -> Probe:
    format_data = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        raise MediaError(422, "INVALID_VIDEO", "视频无法解析。", "processing_failed")
    format_names = frozenset(str(format_data.get("format_name", "")).casefold().split(","))
    if not format_names.intersection({"mov", "mp4", "webm", "matroska"}):
        raise MediaError(415, "UNSUPPORTED_VIDEO_TYPE", "视频格式不受支持。", "processing_failed")
    if mime == "video/webm" and not format_names.intersection({"webm", "matroska"}):
        raise MediaError(
            415, "UNSUPPORTED_VIDEO_TYPE", "视频格式与声明不一致。", "processing_failed"
        )
    if mime in {"video/mp4", "video/quicktime"} and not format_names.intersection({"mov", "mp4"}):
        raise MediaError(
            415, "UNSUPPORTED_VIDEO_TYPE", "视频格式与声明不一致。", "processing_failed"
        )
    videos = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not videos:
        raise MediaError(422, "INVALID_VIDEO", "视频中没有可解码的视频流。", "processing_failed")
    video = videos[0]
    try:
        duration = float(video.get("duration") or format_data.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError):
        raise MediaError(422, "INVALID_VIDEO", "视频元数据无效。", "processing_failed") from None
    fps = _fraction(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"))
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError(422, "INVALID_VIDEO", "视频时长无效。", "processing_failed")
    if duration > MAX_DURATION_SECONDS + 1e-6:
        raise MediaError(413, "VIDEO_TOO_LONG", "视频不能超过 20 秒。", "processing_failed")
    if width < 2 or height < 2:
        raise MediaError(422, "INVALID_VIDEO", "视频尺寸无效。", "processing_failed")
    if width > MAX_WIDTH or height > MAX_HEIGHT or fps > MAX_FPS:
        raise MediaError(
            413, "VIDEO_DIMENSIONS_TOO_LARGE", "视频尺寸或帧率超出限制。", "processing_failed"
        )
    has_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )
    return Probe(format_names, duration, width, height, fps, has_audio)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Durably publish a pair's directory entries on the deployable POSIX host."""

    if os.name == "nt":
        # Windows does not provide the POSIX directory-fsync contract.  The
        # Linux container must take the strict branch below instead.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _write_upload(upload: UploadFile, destination: Path) -> int:
    count = 0
    with destination.open("xb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_INPUT_BYTES:
                raise MediaError(413, "VIDEO_TOO_LARGE", "视频不能超过 50 MB。")
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return count


def _output_probe_is_fixed(payload: dict[str, Any]) -> bool:
    return _output_duration_ms(payload) is not None and _output_frame_count(payload) is not None


def _new_output_probe_is_valid(payload: dict[str, Any]) -> bool:
    return _output_probe_is_fixed(payload) and _output_video_dimensions(payload) == (
        MAX_OUTPUT_WIDTH,
        MAX_OUTPUT_HEIGHT,
    )


def _output_dimensions_are_valid(width: int, height: int) -> bool:
    """Validate the dimensions shared by the new and legacy output contracts."""

    return (
        isinstance(width, int)
        and not isinstance(width, bool)
        and isinstance(height, int)
        and not isinstance(height, bool)
        and 2 <= width <= MAX_OUTPUT_WIDTH
        and 2 <= height <= MAX_OUTPUT_HEIGHT
        and width % 2 == 0
        and height % 2 == 0
    )


def _output_video_dimensions(payload: dict[str, Any]) -> tuple[int, int] | None:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(videos) != 1:
        return None
    try:
        width = int(videos[0].get("width"))
        height = int(videos[0].get("height"))
    except (TypeError, ValueError, OverflowError):
        return None
    return (width, height) if _output_dimensions_are_valid(width, height) else None


def _output_duration_ms(payload: dict[str, Any]) -> int | None:
    format_data = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        return None
    format_names = {
        part.strip().casefold() for part in str(format_data.get("format_name", "")).split(",")
    }
    if "mp4" not in format_names:
        return None
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audios = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(streams) != 2 or len(videos) != 1 or len(audios) != 1:
        return None
    video = videos[0]
    audio = audios[0]
    try:
        duration = float(video.get("duration"))
        channels = int(audio.get("channels"))
        sample_rate = int(audio.get("sample_rate"))
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        width = int(video.get("width"))
        height = int(video.get("height"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION_SECONDS + 1e-6:
        return None
    frame_rates = []
    for value in (video.get("avg_frame_rate"), video.get("r_frame_rate")):
        if value not in (None, "", "N/A"):
            frame_rates.append(_fraction(str(value)))
    try:
        duration_ms = round(duration * 1000)
    except (OverflowError, ValueError):
        return None
    if not 1 <= duration_ms <= 20_000:
        return None
    if (
        video.get("codec_name") != "h264"
        or video.get("pix_fmt") != "yuv420p"
        or not _output_dimensions_are_valid(width, height)
        or not frame_rates
        or any(abs(rate - 30.0) > 1e-6 for rate in frame_rates)
        or audio.get("codec_name") != "aac"
        or sample_rate != 48_000
        or channels != 2
    ):
        return None
    return duration_ms


def _output_frame_count(payload: dict[str, Any]) -> int | None:
    """Return the measured output video frame count, never a duration estimate."""

    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(videos) != 1:
        return None
    raw_count = videos[0].get("nb_frames")
    if isinstance(raw_count, bool) or raw_count is None:
        return None
    text = str(raw_count).strip()
    if not text.isdecimal():
        return None
    try:
        count = int(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 1 else None


def _poster_frame_index(payload: dict[str, Any]) -> int | None:
    """Choose a zero-based frame at or before the prescribed cover target."""

    duration_ms = _output_duration_ms(payload)
    frame_count = _output_frame_count(payload)
    if duration_ms is None or frame_count is None:
        return None
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(videos) != 1:
        return None
    try:
        duration = float(videos[0].get("duration"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    target_seconds = duration / 2 if duration < 1.0 else 1.0
    target_index = math.floor(target_seconds * 30.0)
    return min(target_index, frame_count - 1)


def _poster_probe_is_valid(payload: dict[str, Any]) -> bool:
    format_data = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        return False
    format_names = {
        part.strip().casefold() for part in str(format_data.get("format_name", "")).split(",")
    }
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if (
        not format_names.intersection({"webp", "webp_pipe", "image2"})
        or len(streams) != 1
        or len(videos) != 1
    ):
        return False
    video = videos[0]
    try:
        width = int(video.get("width"))
        height = int(video.get("height"))
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        video.get("codec_name") == "webp"
        and 2 <= width <= MAX_POSTER_WIDTH
        and width % 2 == 0
        and height >= 2
        and height % 2 == 0
    )


def _poster_height_for_width(video_width: int, video_height: int, poster_width: int) -> int:
    """Mirror FFmpeg scale's positive ``h=-2`` nearest/tie-away rounding."""

    # FFmpeg uses av_rescale(poster_width, video_height, video_width * 2) * 2.
    # For positive values and this even denominator, adding half the
    # denominator implements AV_ROUND_NEAR_INF without floating point or
    # Python's bankers-rounding behavior.
    rounded_units = (poster_width * video_height + video_width) // (video_width * 2)
    return max(2, rounded_units * 2)


def _poster_dimensions_match_video(
    poster_payload: dict[str, Any],
    video_payload: dict[str, Any],
    *,
    allow_legacy: bool = True,
) -> bool:
    """Check a poster's dimensions against its output video.

    A 1280x720 video can be either a legacy 640px-poster pair or a newly
    generated 1280px-poster pair.  The video dimensions alone cannot identify
    which version it is, so both complete contracts are accepted for this one
    shared shape.  All other new outputs must use the corresponding 1280px
    (or source-width) poster.
    """

    video_dimensions = _output_video_dimensions(video_payload)
    if video_dimensions is None:
        return False
    streams = poster_payload.get("streams")
    if not isinstance(streams, list):
        return False
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(videos) != 1:
        return False
    try:
        poster_width = int(videos[0].get("width"))
        poster_height = int(videos[0].get("height"))
    except (TypeError, ValueError, OverflowError):
        return False
    video_width, video_height = video_dimensions
    allowed_widths = {min(MAX_POSTER_WIDTH, video_width)}
    if allow_legacy and video_dimensions == (1280, 720):
        allowed_widths.add(640)
    if poster_width not in allowed_widths or poster_height < 2 or poster_height % 2:
        return False
    expected_height = _poster_height_for_width(video_width, video_height, poster_width)
    return poster_height == expected_height


async def process_upload(
    upload: UploadFile,
    *,
    content_type: str | None,
    rights_note: str,
    lineup_id: uuid.UUID,
    request_id: str,
    commit: Callable[[ProcessedMedia], Awaitable[tuple[str | None, str | None] | None]]
    | None = None,
) -> ProcessedMedia:
    mime = (content_type or "").split(";", 1)[0].strip().casefold()
    if mime not in _VIDEO_MIMES:
        raise MediaError(415, "UNSUPPORTED_VIDEO_TYPE", "视频格式不受支持。")
    rights = rights_note.strip()
    if not 1 <= len(rights) <= 500:
        raise MediaError(422, "VALIDATION_ERROR", "请求参数不合法。")
    if not _REQUEST_ID_RE.fullmatch(request_id):
        request_id = uuid.uuid4().hex
    settings = get_settings()
    root = Path(settings.media_store_root)
    temp_root = Path(settings.temp_root)
    if not _prepare_safe_directory(root, create=True):
        raise MediaError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
    if not _prepare_safe_directory(temp_root, create=True):
        raise MediaError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
    staging_root = root / ".staging"
    if not _prepare_safe_directory(staging_root, create=True):
        raise MediaError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
    staging = root / ".staging" / request_id
    source = temp_root / f"upload-{request_id}-{uuid.uuid4().hex}.bin"
    if staging.exists() or staging.is_symlink():
        raise MediaError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        await _write_upload(upload, source)
        deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS

        def remaining_budget() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return remaining

        try:
            initial = _validate_probe(await _probe(source, timeout=remaining_budget()), mime)
        except TimeoutError:
            raise MediaError(
                504,
                "VIDEO_PROCESSING_TIMEOUT",
                "视频处理超时。",
                "processing_timeout",
                persist_failure=True,
            ) from None
        except subprocess.CalledProcessError as exc:
            raise MediaError(422, "INVALID_VIDEO", "视频无法解析。", "processing_failed") from exc
        acquired = False
        if _TRANSCODE_SLOT.locked():
            raise MediaError(409, "TRANSCODE_BUSY", "视频处理中，请稍后重试。")
        try:
            await asyncio.wait_for(_TRANSCODE_SLOT.acquire(), timeout=0.01)
            acquired = True
        except TimeoutError:
            raise MediaError(409, "TRANSCODE_BUSY", "视频处理中，请稍后重试。") from None
        try:
            storage_key = uuid.uuid4()
            output = staging / f"primary-{storage_key}.mp4"
            poster = staging / f"primary-{storage_key}.webp"
            duration_text = f"{initial.duration:.6f}"
            filter_graph = (
                "split=2[bg][fg];"
                "[bg]scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,"
                "crop=1920:1080,boxblur=40:2,"
                "eq=brightness=-0.18:saturation=0.75[bgfill];"
                "[fg]scale=w='min(1920,iw)':h='min(1080,ih)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos[fgfit];"
                "[bgfill][fgfit]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30"
            )

            def transcode_command(crf: int) -> list[str]:
                if initial.has_audio:
                    command = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0",
                    ]
                else:
                    command = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-map",
                        "1:v:0",
                    ]
                command.extend(
                    [
                        "-vf",
                        filter_graph,
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-threads",
                        "2",
                        "-crf",
                        str(crf),
                        "-maxrate",
                        "4000k",
                        "-bufsize",
                        "8000k",
                        "-pix_fmt",
                        "yuv420p",
                        "-r",
                        "30",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "96k",
                        "-ar",
                        "48000",
                        "-ac",
                        "2",
                        "-map_metadata",
                        "-1",
                        "-map_chapters",
                        "-1",
                        "-t",
                        duration_text,
                        "-movflags",
                        "+faststart",
                        str(output),
                    ]
                )
                return command

            try:
                await _run_process(transcode_command(20), remaining_budget())
                if not _ensure_not_symlink(output):
                    raise MediaError(
                        422,
                        "VIDEO_PROCESSING_FAILED",
                        "视频输出无效。",
                        "processing_failed",
                        persist_failure=True,
                    )
                try:
                    output_size = output.stat().st_size
                except OSError as exc:
                    raise MediaError(
                        422,
                        "VIDEO_PROCESSING_FAILED",
                        "视频输出无效。",
                        "processing_failed",
                        persist_failure=True,
                    ) from exc
                if output_size > MAX_OUTPUT_BYTES:
                    try:
                        if not _ensure_not_symlink(output):
                            raise OSError(errno.ELOOP, "unsafe output candidate")
                        output.unlink()
                    except OSError as exc:
                        raise MediaError(
                            422,
                            "VIDEO_PROCESSING_FAILED",
                            "视频输出无效。",
                            "processing_failed",
                            persist_failure=True,
                        ) from exc
                    await _run_process(transcode_command(22), remaining_budget())
                    if not _ensure_not_symlink(output):
                        raise MediaError(
                            422,
                            "VIDEO_PROCESSING_FAILED",
                            "视频输出无效。",
                            "processing_failed",
                            persist_failure=True,
                        )
                    try:
                        output_size = output.stat().st_size
                    except OSError as exc:
                        raise MediaError(
                            422,
                            "VIDEO_PROCESSING_FAILED",
                            "视频输出无效。",
                            "processing_failed",
                            persist_failure=True,
                        ) from exc
                    if output_size > MAX_OUTPUT_BYTES:
                        raise MediaError(
                            413,
                            "VIDEO_OUTPUT_TOO_LARGE",
                            "处理后的视频不能超过 12 MB。",
                            "output_too_large",
                            persist_failure=True,
                        )
                try:
                    output_probe = await _probe(output, timeout=remaining_budget())
                except TimeoutError:
                    raise MediaError(
                        504,
                        "VIDEO_PROCESSING_TIMEOUT",
                        "视频处理超时。",
                        "processing_timeout",
                        persist_failure=True,
                    ) from None
                except (subprocess.CalledProcessError, MediaError) as exc:
                    raise MediaError(
                        422,
                        "VIDEO_PROCESSING_FAILED",
                        "视频输出无效。",
                        "processing_failed",
                        persist_failure=True,
                    ) from exc
                if not _new_output_probe_is_valid(output_probe):
                    raise MediaError(
                        422,
                        "VIDEO_PROCESSING_FAILED",
                        "视频输出参数无效。",
                        "processing_failed",
                        persist_failure=True,
                    )
                output_duration_ms = _output_duration_ms(output_probe)
                frame_index = _poster_frame_index(output_probe)
                if output_duration_ms is None or frame_index is None:
                    raise MediaError(
                        422,
                        "VIDEO_PROCESSING_FAILED",
                        "视频输出时长或帧数无效。",
                        "processing_failed",
                        persist_failure=True,
                    )
                select_filter = (
                    f"select=eq(n\\,{frame_index}),scale=w='min(1280,iw)':h=-2:flags=lanczos"
                )
                await _run_process(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(output),
                        "-map",
                        "0:v:0",
                        "-vf",
                        select_filter,
                        "-frames:v",
                        "1",
                        "-vsync",
                        "0",
                        "-an",
                        "-c:v",
                        "libwebp",
                        "-q:v",
                        "85",
                        str(poster),
                    ],
                    remaining_budget(),
                )
                poster_probe = await _probe(poster, timeout=remaining_budget())
                if not _poster_probe_is_valid(poster_probe) or not _poster_dimensions_match_video(
                    poster_probe, output_probe, allow_legacy=False
                ):
                    raise MediaError(
                        422,
                        "VIDEO_PROCESSING_FAILED",
                        "视频封面无效。",
                        "processing_failed",
                        persist_failure=True,
                    )
                await _run_process(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(poster),
                        "-f",
                        "null",
                        "-",
                    ],
                    remaining_budget(),
                )
            except TimeoutError:
                raise MediaError(
                    504,
                    "VIDEO_PROCESSING_TIMEOUT",
                    "视频处理超时。",
                    "processing_timeout",
                    persist_failure=True,
                ) from None
            except (asyncio.CancelledError, subprocess.CalledProcessError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise MediaError(
                    422,
                    "VIDEO_PROCESSING_FAILED",
                    "视频处理失败。",
                    "processing_failed",
                    persist_failure=True,
                ) from exc
            if not _ensure_not_symlink(output) or not _ensure_not_symlink(poster):
                raise MediaError(
                    422,
                    "VIDEO_PROCESSING_FAILED",
                    "视频输出无效。",
                    "processing_failed",
                    persist_failure=True,
                )
            _fsync_file(output)
            _fsync_file(poster)
            relative_dir = Path(str(lineup_id))
            public_dir = root / "public" / relative_dir
            if not _prepare_safe_directory(public_dir, create=True):
                raise MediaError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
            final_video = public_dir / output.name
            final_poster = public_dir / poster.name
            checksum = hashlib.sha256(output.read_bytes()).hexdigest()
            processed = ProcessedMedia(
                storage_key,
                f"{lineup_id}/{output.name}",
                f"{lineup_id}/{poster.name}",
                output_duration_ms,
                output.stat().st_size,
                checksum,
            )
            file_journal: list[tuple[Path, Path]] = []
            db_committed = False
            previous_paths: tuple[str | None, str | None] | None = None

            def rollback_file_moves() -> None:
                for source_path, destination_path in reversed(file_journal):
                    if not destination_path.exists():
                        continue
                    if source_path.exists():
                        destination_path.unlink(missing_ok=True)
                    else:
                        os.replace(destination_path, source_path)

            try:
                async with media_lock(timeout_seconds=5):
                    try:
                        # The storage key is freshly generated, so refusing an
                        # occupied destination avoids overwriting an unrelated
                        # file and keeps the rollback journal unambiguous.
                        if (
                            final_video.exists()
                            or final_video.is_symlink()
                            or final_poster.exists()
                            or final_poster.is_symlink()
                        ):
                            raise MediaError(
                                500,
                                "VIDEO_PROCESSING_FAILED",
                                "视频处理失败。",
                                "processing_failed",
                                persist_failure=True,
                            )
                        file_journal.append((output, final_video))
                        os.replace(output, final_video)
                        file_journal.append((poster, final_poster))
                        os.replace(poster, final_poster)
                        _fsync_file(final_video)
                        _fsync_file(final_poster)
                        _fsync_directory(public_dir)
                        if commit is not None:
                            commit_task = asyncio.create_task(commit(processed))
                            try:
                                previous_paths, interrupted = await _await_task_settled(commit_task)
                            except (DBAPIError, MediaCommitUnknown) as exc:
                                # A connection/commit exception does not prove
                                # that PostgreSQL rolled back.  Keep both the
                                # new pair and the old pair for reconcile_media.
                                db_committed = True
                                import logging

                                if isinstance(exc, MediaCommitUnknown):
                                    raise exc
                                unavailable = database_is_unavailable(exc)
                                logging.getLogger("smokeline").error(
                                    "media_commit_unknown",
                                    extra={
                                        "event": "media_commit_unknown",
                                        "error_code": (
                                            "DATABASE_UNAVAILABLE"
                                            if unavailable
                                            else "MEDIA_COMMIT_UNKNOWN"
                                        ),
                                    },
                                )
                                if unavailable:
                                    raise MediaCommitUnknown(
                                        503,
                                        "DATABASE_UNAVAILABLE",
                                        "数据库暂时不可用，请稍后重试。",
                                    ) from None
                                raise MediaCommitUnknown from None
                            except asyncio.CancelledError:
                                # A callback that cancels itself may have done
                                # so after its transaction committed.  Treat
                                # that outcome as unknown and never roll back
                                # the files on this path.
                                db_committed = True
                                import logging

                                logging.getLogger("smokeline").error(
                                    "media_commit_unknown",
                                    extra={
                                        "event": "media_commit_unknown",
                                        "error_code": "MEDIA_COMMIT_UNKNOWN",
                                    },
                                )
                                raise
                            # Cancellation is reported only after the callback
                            # has settled.  A successful callback means its
                            # transaction has committed, even if the caller
                            # was cancelled while it was awaiting the result.
                            if interrupted:
                                db_committed = True
                                raise asyncio.CancelledError
                        # The callback returns only after its DB transaction has
                        # committed.  Set this before any post-commit cleanup so
                        # cancellation cannot roll back a pointer that is live.
                        db_committed = True
                    except BaseException:
                        if not db_committed:
                            # Every new-file rename/fsync and its rollback stay
                            # under the same media lock.  This prevents
                            # reconciliation from observing a half-pair while a
                            # failed request unwinds.
                            try:
                                rollback_file_moves()
                            except OSError:
                                # Reconciliation will quarantine an orphaned new
                                # pair on the next startup; never claim its DB
                                # pointer changed.
                                pass
                        raise
                    if previous_paths:
                        try:
                            await move_public_pair_to_trash(previous_paths, assume_locked=True)
                        except OSError:
                            import logging

                            logging.getLogger("smokeline").warning(
                                "media_trash_move_failed",
                                extra={
                                    "event": "media_trash_move_failed",
                                    "error_code": "MEDIA_TRASH_MOVE_FAILED",
                                },
                            )
            except MediaLockTimeout:
                raise MediaBusy from None
            return processed
        finally:
            if acquired:
                _TRANSCODE_SLOT.release()
    finally:
        try:
            source.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass


def _move_one_to_trash(media_root_path: Path, relative: str, day: str) -> None:
    source = _safe_relative_path(media_root_path / "public", relative)
    if source is None or not source.exists() or source.is_symlink() or not source.is_file():
        return
    target = media_root_path / "trash" / day / "public" / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


async def move_public_pair_to_trash(
    paths: tuple[str | None, str | None], *, assume_locked: bool = False
) -> None:
    """Move an old public pair after a successful pointer swap.

    ``assume_locked`` is used only by the upload commit callback, which is
    invoked while the same media lock protects the new-file move and DB
    pointer update.
    """

    from datetime import date

    root = media_root()

    def move() -> None:
        for path in paths:
            if path:
                _move_one_to_trash(root, path, date.today().isoformat())

    if assume_locked:
        move()
    else:
        async with media_lock():
            move()
