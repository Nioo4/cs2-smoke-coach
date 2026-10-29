"""Explicitly purge old regular files from the media trash volume.

The command is intentionally CLI-only.  It makes a complete, fail-closed
deletion plan while holding the media lock, then removes only files in valid
UTC date buckets older than the requested cutoff.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.core.config import ConfigError, Settings, get_settings
from app.services.media import MediaLockTimeout, media_lock

_DATE_BUCKET = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REPARSE_POINT = 0x0400


class PurgeError(RuntimeError):
    """Raised when trash cannot be classified safely."""


def positive_days(value: str) -> int:
    if not value.isdecimal():
        raise argparse.ArgumentTypeError("must be a positive integer")
    days = int(value)
    if days <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return days


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError as exc:
        raise PurgeError("cannot inspect trash object safely") from exc
    return bool(attributes & _REPARSE_POINT)


def _real_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise PurgeError(f"{label} must be an absolute path")
    if _is_reparse_or_symlink(path) or not path.exists() or not path.is_dir():
        raise PurgeError(f"{label} must be an existing real directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PurgeError(f"{label} cannot be resolved safely") from exc
    if _is_reparse_or_symlink(resolved) or not resolved.is_dir():
        raise PurgeError(f"{label} must be an existing real directory")
    return resolved


def _assert_below(path: Path, parent: Path) -> Path:
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(parent)
    except (OSError, ValueError) as exc:
        raise PurgeError("trash path escaped the configured media root") from exc
    return resolved


def _date_bucket(value: str) -> date:
    if not _DATE_BUCKET.fullmatch(value):
        raise PurgeError(f"invalid trash date bucket: {value}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PurgeError(f"invalid trash date bucket: {value}") from exc


def _iter_children(path: Path) -> Iterable[Path]:
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                yield Path(entry.path)
    except OSError as exc:
        raise PurgeError("cannot scan media trash safely") from exc


def _scan_bucket(
    bucket: Path, bucket_day: date, trash: Path, cutoff: date, plan: list[Path]
) -> None:
    def visit(directory: Path, *, relative_depth: int) -> None:
        for child in _iter_children(directory):
            _assert_below(child, trash)
            if _is_reparse_or_symlink(child):
                raise PurgeError("symlink or reparse point found in media trash")
            try:
                is_dir = child.is_dir()
                is_file = child.is_file()
            except OSError as exc:
                raise PurgeError("cannot classify media trash object") from exc
            if is_dir:
                if relative_depth == 0 and child.name not in {"public", "archive"}:
                    raise PurgeError("unexpected media trash source root")
                visit(child, relative_depth=relative_depth + 1)
                continue
            if not is_file:
                raise PurgeError("unknown object found in media trash")
            # Existing writers store trash/YYYY-MM-DD/{public|archive}/lineup/file.
            # A file directly below the date bucket or source-root is malformed.
            if relative_depth < 2:
                raise PurgeError("unexpected media trash structure")
            if bucket_day < cutoff:
                plan.append(child)

    visit(bucket, relative_depth=0)


def build_deletion_plan(
    media_root: str | Path, older_than_days: int, *, now: date | None = None
) -> list[Path]:
    """Scan all trash first and return a safe, deterministic file-only plan."""

    if isinstance(older_than_days, bool) or older_than_days <= 0:
        raise PurgeError("older_than_days must be a positive integer")
    root_path = Path(media_root)
    if not root_path.is_absolute():
        raise PurgeError("MEDIA_STORE_ROOT must be an absolute path")
    root = _real_directory(root_path, label="MEDIA_STORE_ROOT")
    trash_path = root / "trash"
    _assert_below(trash_path, root)
    if not trash_path.exists() and not trash_path.is_symlink():
        return []
    trash = _real_directory(trash_path, label="trash")
    try:
        if trash.parent != root:
            raise PurgeError("trash must be a direct child of MEDIA_STORE_ROOT")
    except OSError as exc:
        raise PurgeError("trash path cannot be classified safely") from exc
    today = now or datetime.now(UTC).date()
    cutoff = today - timedelta(days=older_than_days)
    plan: list[Path] = []
    for bucket in _iter_children(trash):
        _assert_below(bucket, trash)
        if _is_reparse_or_symlink(bucket):
            raise PurgeError("symlink or reparse point found in media trash")
        if not bucket.is_dir():
            raise PurgeError("trash contains a non-directory date bucket")
        bucket_day = _date_bucket(bucket.name)
        _scan_bucket(bucket, bucket_day, trash, cutoff, plan)
    return sorted(plan, key=lambda item: item.as_posix())


def execute_deletion_plan(plan: list[Path], media_root: str | Path) -> int:
    """Delete only the already-scanned ordinary files, rechecking each race."""

    root = _real_directory(Path(media_root), label="MEDIA_STORE_ROOT")
    trash_path = root / "trash"
    if not trash_path.exists() and not trash_path.is_symlink():
        return 0
    trash = _real_directory(trash_path, label="trash")
    deleted = 0
    for path in plan:
        _assert_below(path, trash)
        if _is_reparse_or_symlink(path):
            raise PurgeError("trash object changed to a symlink or reparse point")
        try:
            if not path.exists() or not path.is_file():
                raise PurgeError("trash file changed before deletion")
            path.unlink()
        except PurgeError:
            raise
        except OSError as exc:
            raise PurgeError(f"failed to delete trash file: {path.name}") from exc
        deleted += 1
    return deleted


async def purge(settings: Settings, older_than_days: int, *, now: date | None = None) -> int:
    root = Path(settings.media_store_root)
    async with media_lock(timeout_seconds=5):
        plan = build_deletion_plan(root, older_than_days, now=now)
        return execute_deletion_plan(plan, root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Purge old files from media trash.")
    parser.add_argument("--older-than-days", required=True, type=positive_days)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        settings = get_settings()
        deleted = asyncio.run(purge(settings, args.older_than_days))
    except SystemExit:
        raise
    except (ConfigError, MediaLockTimeout, PurgeError) as exc:
        print(f"PURGE_MEDIA_TRASH_FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("PURGE_MEDIA_TRASH_FAILED: unable to classify trash safely", file=sys.stderr)
        return 1
    print(f"PURGE_MEDIA_TRASH deleted={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
