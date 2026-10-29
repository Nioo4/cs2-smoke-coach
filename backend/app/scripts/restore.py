"""Validate and restore a backup only into an explicitly isolated target.

This module is a local operator tool, not an API.  Its default action is a
read-only backup/scope check.  Database and media writes require an explicit
``--execute`` and a project name in the ``smokeline-restore-*`` namespace.
Production paths, markers, ports, and Compose files are rejected before any
archive extraction or ``pg_restore`` invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

_PROJECT_RE = re.compile(r"^smokeline-restore-[a-z0-9][a-z0-9-]{0,40}$")
_COMPOSE_PUBLIC_PORT_RE = re.compile(
    r"""(?imx)
    (?:^|[\s\[,])["']?
    (?:(?:(?:\d{1,3}\.){3}\d{1,3}|\[[^]\r\n]+\]):)?
    (?:80|443):
    \d{1,5}(?:/(?:tcp|udp|sctp))?
    ["']?(?=\s*(?:$|[,\]#]))
    """
)
_RELATIVE_REJECT = {"", ".", ".."}
_PRODUCTION_PARTS = {
    ".env",
    "admin.txt",
    "production",
    "prod",
    "live",
    "secrets",
    "smokeline-release",
}


class RestoreError(RuntimeError):
    """The requested restore is unsafe or the backup is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RestoreError("restore checksum failed") from exc
    return digest.hexdigest()


def _safe_member_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or "\\" in name or name.startswith("/"):
        raise RestoreError("unsafe archive path")
    parts = name.split("/")
    if any(part in _RELATIVE_REJECT for part in parts):
        raise RestoreError("unsafe archive path")
    candidate = root.joinpath(*parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RestoreError("archive path escaped target") from exc
    return candidate


def validate_restore_scope(
    *,
    project: str,
    restore_root: Path,
    media_root: Path,
    backup_path: Path,
    compose_file: Path,
    port: int,
) -> None:
    if not _PROJECT_RE.fullmatch(project) or project in {"smokeline", "smokeline-prod"}:
        raise RestoreError("RESTORE_PRODUCTION_SCOPE_REJECTED")
    if not 18080 <= port <= 18999:
        raise RestoreError("RESTORE_PORT_REJECTED")
    for path in (restore_root, media_root, backup_path):
        if not path.is_absolute() or path.is_symlink():
            raise RestoreError("RESTORE_PATH_REJECTED")
        parts = {part.casefold() for part in path.parts}
        if parts & _PRODUCTION_PARTS or any(
            part.startswith(("smokeline-prod", "smokeline-production", "smokeline-release"))
            for part in parts
        ):
            raise RestoreError("RESTORE_PRODUCTION_PATH_REJECTED")
    try:
        restore_root_resolved = restore_root.resolve(strict=False)
        media_root_resolved = media_root.resolve(strict=False)
        media_root_resolved.relative_to(restore_root_resolved)
    except (OSError, ValueError) as exc:
        raise RestoreError("RESTORE_MEDIA_PATH_REJECTED") from exc
    for forbidden in (Path("/opt/smokeline"), Path("/data/media-store"), Path("/data/backups")):
        for path in (restore_root, media_root, backup_path):
            try:
                path.resolve(strict=False).relative_to(forbidden)
            except ValueError:
                continue
            raise RestoreError("RESTORE_PRODUCTION_PATH_REJECTED")
    if (restore_root / ".smokeline-production").exists() or (restore_root / "RELEASE").exists():
        raise RestoreError("RESTORE_PRODUCTION_MARKER_REJECTED")
    if not compose_file.is_absolute() or compose_file.is_symlink() or not compose_file.is_file():
        raise RestoreError("RESTORE_COMPOSE_REJECTED")
    if compose_file.name.casefold() != "compose.restore.yml":
        raise RestoreError("RESTORE_PRODUCTION_COMPOSE_REJECTED")
    try:
        compose_text = compose_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RestoreError("RESTORE_COMPOSE_REJECTED") from exc
    lowered = compose_text.casefold()
    if (
        "compose.prod" in lowered
        or "/opt/smokeline" in lowered
        or "/etc/letsencrypt" in lowered
        or ".env" in lowered
        or _COMPOSE_PUBLIC_PORT_RE.search(compose_text)
    ):
        raise RestoreError("RESTORE_PRODUCTION_COMPOSE_REJECTED")
    marker = os.environ.get("SMOKELINE_RESTORE_PROJECT")
    if marker != project:
        raise RestoreError("RESTORE_ISOLATION_MARKER_REJECTED")
    if "com.smokeline.restore.project" not in lowered or "${restore_project" not in lowered:
        raise RestoreError("RESTORE_ISOLATION_MARKER_REJECTED")
    if "app_env: test" not in lowered or "ai_provider: disabled" not in lowered:
        raise RestoreError("RESTORE_NONPRODUCTION_ENV_REJECTED")
    if str(port) not in compose_text:
        raise RestoreError("RESTORE_PORT_NOT_DECLARED")


def _validate_file_entry(group: Path, entry: dict[str, Any], *, key: str) -> Path:
    expected_keys = {
        "database": {"path", "format", "size_bytes", "sha256"},
        "media": {"path", "size_bytes", "sha256"},
    }[key]
    if set(entry) != expected_keys:
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    path_value = entry.get("path")
    size_value = entry.get("size_bytes")
    checksum = entry.get("sha256")
    if not isinstance(path_value, str) or "\\" in path_value or path_value.startswith("/"):
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    if not isinstance(size_value, int) or size_value < 0:
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    path = _safe_member_path(group, path_value)
    if not path.is_file() or path.is_symlink():
        raise RestoreError("RESTORE_ARTIFACT_MISSING")
    if path.stat().st_size != size_value or _sha256(path) != checksum:
        raise RestoreError("RESTORE_CHECKSUM_MISMATCH")
    return path


def validate_backup_group(group: Path) -> dict[str, Any]:
    if not group.is_absolute() or group.is_symlink() or not group.is_dir():
        raise RestoreError("RESTORE_BACKUP_PATH_REJECTED")
    manifest_path = group / "manifest.json"
    checksums_path = group / "checksums.sha256"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    if checksums_path.is_symlink():
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreError("RESTORE_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "format_version",
        "created_at",
        "database",
        "media",
        "media_files",
    }:
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("created_at"), str):
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    database = manifest.get("database")
    media = manifest.get("media")
    media_files = manifest.get("media_files")
    if (
        not isinstance(database, dict)
        or not isinstance(media, dict)
        or not isinstance(media_files, list)
    ):
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    _validate_file_entry(group, database, key="database")
    media_path = _validate_file_entry(group, media, key="media")
    if database.get("format") != "custom" or database.get("path", "").startswith("db/") is False:
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    if media.get("path") != "media.tar":
        raise RestoreError("RESTORE_MANIFEST_INVALID")
    listed: dict[str, dict[str, Any]] = {}
    for entry in media_files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size_bytes", "sha256"}:
            raise RestoreError("RESTORE_MANIFEST_INVALID")
        path = entry.get("path")
        if not isinstance(path, str) or (
            not path.startswith("public/") and not path.startswith("archive/")
        ):
            raise RestoreError("RESTORE_MANIFEST_INVALID")
        if path in listed:
            raise RestoreError("RESTORE_MANIFEST_INVALID")
        _safe_member_path(group, path)
        size_value = entry.get("size_bytes")
        checksum = entry.get("sha256")
        if (
            not isinstance(size_value, int)
            or size_value < 0
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise RestoreError("RESTORE_MANIFEST_INVALID")
        listed[path] = entry
    try:
        checksum_lines = checksums_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RestoreError("RESTORE_CHECKSUM_LIST_INVALID") from exc
    expected_lines = {
        database.get("path"): database.get("sha256"),
        media.get("path"): media.get("sha256"),
        "manifest.json": _sha256(manifest_path),
    }
    observed: dict[str, str] = {}
    for line in checksum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise RestoreError("RESTORE_CHECKSUM_LIST_INVALID")
        if parts[1] in observed:
            raise RestoreError("RESTORE_CHECKSUM_LIST_INVALID")
        observed[parts[1]] = parts[0]
    if observed != expected_lines:
        raise RestoreError("RESTORE_CHECKSUM_LIST_INVALID")
    try:
        with tarfile.open(media_path, mode="r") as archive:
            members = archive.getmembers()
            observed_media: set[str] = set()
            for member in members:
                if member.issym() or member.islnk() or not member.isfile():
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_REJECTED")
                _safe_member_path(group, member.name)
                if not (member.name.startswith("public/") or member.name.startswith("archive/")):
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_REJECTED")
                if member.name in observed_media or member.name not in listed:
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_MISMATCH")
                source = archive.extractfile(member)
                if source is None:
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_REJECTED")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                entry = listed[member.name]
                if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise RestoreError("RESTORE_CHECKSUM_MISMATCH")
                observed_media.add(member.name)
            if observed_media != set(listed):
                raise RestoreError("RESTORE_ARCHIVE_MEMBER_MISMATCH")
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError("RESTORE_ARCHIVE_INVALID") from exc
    return manifest


def restore_media_archive(group: Path, destination: Path) -> None:
    manifest = validate_backup_group(group)
    if destination.is_symlink() or not destination.is_absolute():
        raise RestoreError("RESTORE_MEDIA_PATH_REJECTED")
    if destination.exists():
        if not destination.is_dir():
            raise RestoreError("RESTORE_MEDIA_PATH_REJECTED")
        if any(destination.iterdir()):
            raise RestoreError("RESTORE_MEDIA_DESTINATION_NOT_EMPTY")
        destination_created = False
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700)
        destination_created = True
    os.chmod(destination, 0o700)
    # Keep staging below destination so a Docker named-volume mountpoint is
    # never replaced or removed during commit.
    staging = destination / f".restore-{uuid.uuid4().hex}"
    if staging.exists() or staging.is_symlink():
        raise RestoreError("RESTORE_MEDIA_PATH_REJECTED")
    staging.mkdir(mode=0o700)
    os.chmod(staging, 0o700)
    completed = False
    committed: list[Path] = []
    media_path = group / manifest["media"]["path"]
    expected_media = {entry["path"]: entry for entry in manifest["media_files"]}
    observed_media: set[str] = set()
    try:
        with tarfile.open(media_path, mode="r") as archive:
            for member in archive.getmembers():
                target = _safe_member_path(staging, member.name)
                if member.issym() or member.islnk() or not member.isfile():
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_REJECTED")
                expected = expected_media.get(member.name)
                if expected is None or member.name in observed_media:
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_MISMATCH")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(target.parent, 0o700)
                if target.exists() or target.is_symlink():
                    raise RestoreError("RESTORE_MEDIA_DESTINATION_NOT_EMPTY")
                source = archive.extractfile(member)
                if source is None:
                    raise RestoreError("RESTORE_ARCHIVE_MEMBER_REJECTED")
                digest = hashlib.sha256()
                size = 0
                with target.open("xb") as handle:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
                    raise RestoreError("RESTORE_CHECKSUM_MISMATCH")
                # Never inherit archive modes.  Only ``public/`` is exposed
                # through the Nginx media alias after an isolated restore.
                os.chmod(target, 0o644 if member.name.startswith("public/") else 0o640)
                observed_media.add(member.name)
            if observed_media != set(expected_media):
                raise RestoreError("RESTORE_ARCHIVE_MEMBER_MISMATCH")
        # The destination is also the root of a Docker named volume.  Make
        # every public directory traversable, without widening archive access.
        for member_name in expected_media:
            if not member_name.startswith("public/"):
                continue
            public_dir = _safe_member_path(staging, member_name).parent
            while public_dir != staging:
                os.chmod(public_dir, 0o755)
                public_dir = public_dir.parent
        for child in sorted(staging.iterdir(), key=lambda path: path.name):
            target = destination / child.name
            if target.exists() or target.is_symlink():
                raise RestoreError("RESTORE_MEDIA_DESTINATION_NOT_EMPTY")
            os.replace(child, target)
            committed.append(target)
        staging.rmdir()
        os.chmod(destination, 0o755)
        completed = True
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError("RESTORE_MEDIA_EXTRACTION_FAILED") from exc
    finally:
        if not completed:
            for target in reversed(committed):
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    elif target.exists() or target.is_symlink():
                        target.unlink()
                except OSError:
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            if destination_created:
                try:
                    if destination.is_dir() and not any(destination.iterdir()):
                        destination.rmdir()
                except OSError:
                    pass


def restore_database(group: Path, database_url: str) -> None:
    parsed = make_url(database_url)
    if (
        parsed.drivername not in {"postgresql", "postgresql+psycopg"}
        or parsed.host != "postgres"
        or parsed.port not in {None, 5432}
        or parsed.database != "smokeline"
        or parsed.username != "smokeline"
        or not parsed.password
    ):
        raise RestoreError("RESTORE_DATABASE_SCOPE_REJECTED")
    group = group.resolve(strict=True)
    manifest = validate_backup_group(group)
    password = parsed.password
    environment = os.environ.copy()
    environment["PGPASSWORD"] = password
    command = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--single-transaction",
        "--host=postgres",
        "--port=5432",
        "--username=smokeline",
        "--dbname=smokeline",
        str(group / manifest["database"]["path"]),
    ]
    try:
        subprocess.run(
            command,
            env=environment,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RestoreError("pg_restore failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate or restore a SmokeLine backup safely")
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--restore-root", required=True, type=Path)
    parser.add_argument("--media-root", required=True, type=Path)
    parser.add_argument("--compose-file", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--database-url")
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_restore_scope(
            project=args.project,
            restore_root=args.restore_root,
            media_root=args.media_root,
            backup_path=args.backup,
            compose_file=args.compose_file,
            port=args.port,
        )
        validate_backup_group(args.backup)
        if args.execute:
            if not args.database_url:
                raise RestoreError("RESTORE_DATABASE_URL_REQUIRED")
            restore_media_archive(args.backup, args.media_root)
            restore_database(args.backup, args.database_url)
    except (OSError, RestoreError, ValueError):
        print("RESTORE_REJECTED", file=sys.stderr)
        return 2
    print("RESTORE_SCOPE_PASS" if not args.execute else "RESTORE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
