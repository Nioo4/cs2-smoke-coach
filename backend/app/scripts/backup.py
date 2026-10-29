"""Create an atomic, same-disk SmokeLine backup under the media lock.

The command is intentionally a small operational boundary.  It does not
upload backups, expose an HTTP route, or delete media permanently.  A backup
group becomes visible through ``latest`` only after the custom PostgreSQL
dump, media archive, manifest, and checksums have all been fsynced and
verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import (
    ConversationState,
    ExternalSearchCache,
    TutorialVideo,
    UsageEvent,
    VideoStatus,
)
from app.db.session import get_session_factory
from app.scripts.reconcile_media import reconcile_media
from app.services.media import MediaLockTimeout, media_lock

logger = logging.getLogger("smokeline")

RETENTION_COUNT = 7
RETENTION_AGE = timedelta(days=7)
STALE_AGE = timedelta(hours=1)
MIN_FREE_BYTES = 512 * 1024 * 1024
PG_DUMP_TIMEOUT_SECONDS = 120
GROUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


class BackupError(RuntimeError):
    """A backup cannot be completed without weakening its safety contract."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(now: datetime | None = None) -> str:
    return (now or _utc_now()).astimezone(UTC).strftime(GROUP_TIMESTAMP_FORMAT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupError("backup checksum failed") from exc
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    try:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BackupError("backup fsync failed") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # The deployable runtime is Linux; Windows has no portable directory
        # fsync contract, so local checks retain file fsyncs only.
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupError("backup directory fsync failed") from exc


def _write_atomic(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        if path.is_symlink():
            raise BackupError("backup destination is a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError("backup atomic write failed") from exc


def _safe_relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise BackupError("backup path escaped media root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BackupError("unsafe backup relative path")
    return relative.as_posix()


def _iter_media_files(root: Path, directory_name: str) -> list[tuple[str, Path]]:
    directory = root / directory_name
    if directory.is_symlink():
        raise BackupError("media root contains a symlinked directory")
    directory.mkdir(parents=True, exist_ok=True)
    result: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(directory, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            candidate = current_path / name
            if candidate.is_symlink():
                raise BackupError("media root contains a symlink")
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink() or not candidate.is_file():
                raise BackupError("media root contains a non-regular file")
            result.append((_safe_relative(directory, candidate), candidate))
    return sorted(result, key=lambda item: item[0])


async def _referenced_media() -> set[tuple[str, str]]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    TutorialVideo.status,
                    TutorialVideo.relative_path,
                    TutorialVideo.poster_relative_path,
                ).where(TutorialVideo.status.in_([VideoStatus.ready, VideoStatus.archived]))
            )
        ).all()
    referenced: set[tuple[str, str]] = set()
    for status, relative_path, poster_relative_path in rows:
        root_name = "public" if status == VideoStatus.ready else "archive"
        for relative in (relative_path, poster_relative_path):
            if not isinstance(relative, str) or not relative or relative.startswith("/"):
                raise BackupError("database contains unsafe media path")
            referenced.add((root_name, relative.replace("\\", "/")))
    return referenced


def _trash_path(root: Path, source_root: str, relative: str, now: datetime) -> Path:
    trash_root = root / "trash"
    if trash_root.is_symlink() or (trash_root.exists() and not trash_root.is_dir()):
        raise BackupError("unsafe trash root")
    destination = root / "trash" / now.date().isoformat() / source_root / relative
    cursor = trash_root
    for component in destination.relative_to(trash_root).parts:
        cursor /= component
        if cursor.is_symlink():
            raise BackupError("unsafe trash path")
    if destination.exists() or destination.is_symlink():
        destination = destination.with_name(
            f"{destination.stem}.backup-{uuid.uuid4().hex}{destination.suffix}"
        )
    return destination


def _move_stale_media(
    root: Path,
    directory_name: str,
    files: Iterable[tuple[str, Path]],
    referenced: set[tuple[str, str]],
    now: datetime,
) -> int:
    moved = 0
    cutoff = now.timestamp() - STALE_AGE.total_seconds()
    for relative, source in files:
        if (directory_name, relative) in referenced:
            continue
        try:
            stale = source.stat().st_mtime <= cutoff
        except OSError as exc:
            raise BackupError("media stat failed") from exc
        if not stale:
            continue
        if source.is_symlink() or not source.is_file():
            raise BackupError("media changed to non-regular file")
        destination = _trash_path(root, directory_name, relative, now)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError as exc:
            raise BackupError("stale media move failed") from exc
        moved += 1
    return moved


def _move_stale_staging(root: Path, now: datetime) -> int:
    staging = root / ".staging"
    if not staging.exists():
        return 0
    if staging.is_symlink() or not staging.is_dir():
        raise BackupError("unsafe staging root")
    moved = 0
    cutoff = now.timestamp() - STALE_AGE.total_seconds()
    for current, dirs, files in os.walk(staging, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            candidate = current_path / name
            if candidate.is_symlink():
                raise BackupError("staging contains a symlink")
        for name in files:
            source = current_path / name
            if source.is_symlink() or not source.is_file():
                raise BackupError("staging contains a non-regular file")
            try:
                stale = source.stat().st_mtime <= cutoff
            except OSError as exc:
                raise BackupError("staging stat failed") from exc
            if not stale:
                continue
            if source.is_symlink() or not source.is_file():
                raise BackupError("staging changed to non-regular file")
            relative = _safe_relative(staging, source)
            destination = _trash_path(root, ".staging", relative, now)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(source, destination)
            except OSError as exc:
                raise BackupError("stale staging move failed") from exc
            moved += 1
    return moved


async def cleanup_before_dump(now: datetime | None = None) -> dict[str, int]:
    """Clean expired DB rows and stale unreferenced files, without deletion."""

    current = now or _utc_now()
    referenced = await _referenced_media()
    settings = get_settings()
    root = Path(settings.media_store_root)
    public_files = _iter_media_files(root, "public")
    archive_files = _iter_media_files(root, "archive")
    moved = _move_stale_media(root, "public", public_files, referenced, current)
    moved += _move_stale_media(root, "archive", archive_files, referenced, current)
    moved += _move_stale_staging(root, current)
    cutoff = current - timedelta(days=30)
    async with get_session_factory()() as session:
        async with session.begin():
            conversation_result = await session.execute(
                delete(ConversationState).where(ConversationState.expires_at < current)
            )
            cache_result = await session.execute(
                delete(ExternalSearchCache).where(ExternalSearchCache.expires_at < current)
            )
            usage_result = await session.execute(
                delete(UsageEvent).where(UsageEvent.created_at < cutoff)
            )
    return {
        "media_moved_to_trash": moved,
        "conversation_rows": int(conversation_result.rowcount or 0),
        "cache_rows": int(cache_result.rowcount or 0),
        "usage_rows": int(usage_result.rowcount or 0),
    }


async def _database_size_bytes() -> int:
    async with get_session_factory()() as session:
        value = await session.scalar(text("SELECT pg_database_size(current_database())"))
    if value is None:
        raise BackupError("database size unavailable")
    return int(value)


def _media_size_bytes(
    root: Path, files: list[tuple[str, Path]], archive_files: list[tuple[str, Path]]
) -> int:
    total = 0
    for _, path in [*files, *archive_files]:
        try:
            total += path.stat().st_size
        except OSError as exc:
            raise BackupError("media size unavailable") from exc
    return total


def _check_space(backup_root: Path, required_bytes: int) -> None:
    try:
        free = shutil.disk_usage(backup_root).free
    except OSError as exc:
        raise BackupError("backup disk usage unavailable") from exc
    if free < required_bytes:
        raise BackupError("BACKUP_INSUFFICIENT_SPACE")


def _ensure_private_directory(path: Path, *, error: str) -> None:
    """Create one backup-owned directory without following a symlink."""

    if path.is_symlink():
        raise BackupError(error)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise BackupError(error)
        os.chmod(path, 0o700)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(error) from exc


def _pg_dump(settings: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(destination.parent, 0o700)
    except OSError as exc:
        raise BackupError("pg_dump directory permissions failed") from exc
    command = [
        "pg_dump",
        "--format=custom",
        "--host=postgres",
        "--port=5432",
        "--username=smokeline",
        "--dbname=smokeline",
        f"--file={destination}",
    ]
    environment = os.environ.copy()
    environment["PGPASSWORD"] = settings.postgres_password_value
    try:
        subprocess.run(
            command,
            env=environment,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=PG_DUMP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BackupError("pg_dump failed") from exc
    try:
        os.chmod(destination, 0o600)
    except OSError as exc:
        raise BackupError("pg_dump permissions failed") from exc
    _fsync_file(destination)


def _tar_media(
    root: Path,
    destination: Path,
    files: list[tuple[str, Path]],
    archive_files: list[tuple[str, Path]],
) -> list[dict[str, Any]]:
    manifest_files: list[dict[str, Any]] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(destination, mode="w") as archive:
            for root_name, entries in (("public", files), ("archive", archive_files)):
                for relative, path in entries:
                    if path.is_symlink() or not path.is_file():
                        raise BackupError("media changed during archive")
                    archive.add(path, arcname=f"{root_name}/{relative}", recursive=False)
                    manifest_files.append(
                        {
                            "path": f"{root_name}/{relative}",
                            "size_bytes": path.stat().st_size,
                            "sha256": _sha256(path),
                        }
                    )
    except (OSError, tarfile.TarError) as exc:
        raise BackupError("media archive failed") from exc
    try:
        os.chmod(destination, 0o600)
    except OSError as exc:
        raise BackupError("media archive permissions failed") from exc
    _fsync_file(destination)
    return manifest_files


def _write_manifest(group: Path, created_at: datetime, files: list[dict[str, Any]]) -> None:
    group_stem = group.name
    if group_stem.startswith(".") and group_stem.endswith(".incomplete"):
        group_stem = group_stem[1 : -len(".incomplete")]
    database = group / "db" / f"smokeline-{group_stem}.dump"
    media = group / "media.tar"
    manifest = {
        "format_version": 1,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "database": {
            "path": f"db/{database.name}",
            "format": "custom",
            "size_bytes": database.stat().st_size,
            "sha256": _sha256(database),
        },
        "media": {
            "path": "media.tar",
            "size_bytes": media.stat().st_size,
            "sha256": _sha256(media),
        },
        "media_files": files,
    }
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    _write_atomic(group / "manifest.json", payload.encode("utf-8"))
    checksums = (
        "\n".join(
            f"{_sha256(group / relative)}  {relative}"
            for relative in (manifest["database"]["path"], "media.tar", "manifest.json")
        )
        + "\n"
    )
    _write_atomic(group / "checksums.sha256", checksums.encode("ascii"))


def _complete_group(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        # Import lazily to keep the backup module's normal import graph small.
        from app.scripts.restore import validate_backup_group

        validate_backup_group(path)
    except Exception:
        return False
    return True


def retain_backups(backup_root: Path, *, now: datetime | None = None) -> list[str]:
    current = now or _utc_now()
    groups = (
        [path for path in backup_root.iterdir() if _complete_group(path)]
        if backup_root.exists()
        else []
    )
    groups.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    retained = groups[:RETENTION_COUNT]
    retained_names = {path.name for path in retained}
    cutoff = current.timestamp() - RETENTION_AGE.total_seconds()
    for path in groups[RETENTION_COUNT:]:
        try:
            too_old = path.stat().st_mtime < cutoff
        except OSError as exc:
            raise BackupError("backup retention stat failed") from exc
        if too_old or len(groups) > RETENTION_COUNT:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                raise BackupError("backup retention cleanup failed") from exc
    return sorted(retained_names)


async def create_backup(*, now: datetime | None = None) -> Path:
    settings = get_settings()
    current = now or _utc_now()
    backup_root = Path(settings.backup_root)
    media_root = Path(settings.media_store_root)
    _ensure_private_directory(backup_root, error="unsafe backup root")
    media_root.mkdir(parents=True, exist_ok=True)
    group_name = _timestamp(current)
    temporary_group = backup_root / f".{group_name}.incomplete"
    final_group = backup_root / group_name
    if temporary_group.exists() or final_group.exists():
        raise BackupError("backup timestamp collision")
    final_group_created = False
    final_group_validated = False
    try:
        async with media_lock(timeout_seconds=120):
            await reconcile_media()
            await cleanup_before_dump(current)
            public_files = _iter_media_files(media_root, "public")
            archive_files = _iter_media_files(media_root, "archive")
            database_size = await _database_size_bytes()
            media_size = _media_size_bytes(media_root, public_files, archive_files)
            _check_space(backup_root, database_size + media_size + MIN_FREE_BYTES)
            _ensure_private_directory(temporary_group, error="unsafe backup staging group")
            dump_path = temporary_group / "db" / f"smokeline-{group_name}.dump"
            _pg_dump(settings, dump_path)
            media_tar = temporary_group / "media.tar"
            media_manifest = _tar_media(media_root, media_tar, public_files, archive_files)
            _write_manifest(temporary_group, current, media_manifest)
            _fsync_directory(temporary_group)
            os.replace(temporary_group, final_group)
            final_group_created = True
            _fsync_directory(backup_root)
            if not _complete_group(final_group):
                raise BackupError("backup self-validation failed")
            final_group_validated = True
            retain_backups(backup_root, now=current)
            _write_atomic(backup_root / "latest", (group_name + "\n").encode("ascii"))
            return final_group
    except (MediaLockTimeout, BackupError):
        if temporary_group.exists():
            shutil.rmtree(temporary_group, ignore_errors=True)
        if final_group_created and not final_group_validated:
            if final_group.is_symlink() or not final_group.is_dir():
                raise BackupError("unsafe failed backup group") from None
            shutil.rmtree(final_group, ignore_errors=False)
        raise
    except Exception as exc:
        if temporary_group.exists():
            shutil.rmtree(temporary_group, ignore_errors=True)
        if final_group_created and not final_group_validated:
            if final_group.is_symlink() or not final_group.is_dir():
                raise BackupError("unsafe failed backup group") from exc
            shutil.rmtree(final_group, ignore_errors=True)
        raise BackupError("backup failed") from exc


def main() -> int:
    configure_logging()
    try:
        path = asyncio.run(create_backup())
    except Exception as exc:
        error_code = "BACKUP_FAILED"
        if isinstance(exc, MediaLockTimeout):
            error_code = "BACKUP_LOCK_TIMEOUT"
        elif isinstance(exc, BackupError) and str(exc) == "BACKUP_INSUFFICIENT_SPACE":
            error_code = "BACKUP_INSUFFICIENT_SPACE"
        logger.error(
            "backup failed",
            extra={"event": "BACKUP_FAILED", "error_code": error_code},
        )
        return 1
    logger.info(
        "backup complete",
        extra={"event": "BACKUP_COMPLETE", "error_code": None, "backup_group": path.name},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
