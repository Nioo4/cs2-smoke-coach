"""Safety and boundary tests for the explicit media-trash CLI operation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.scripts import purge_media_trash


def _file(root: Path, bucket: str, name: str = "lineup/video.mp4") -> Path:
    path = root / "trash" / bucket / "public" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return path


def test_old_new_and_boundary_files_use_strict_utc_cutoff(tmp_path: Path) -> None:
    root = tmp_path / "media-store"
    root.mkdir()
    old = _file(root, "2026-09-05")
    boundary = _file(root, "2026-09-06", "lineup/boundary.mp4")
    new = _file(root, "2026-09-07", "lineup/new.mp4")
    plan = purge_media_trash.build_deletion_plan(root, 7, now=date(2026, 9, 13))
    assert plan == [old]
    assert purge_media_trash.execute_deletion_plan(plan, root) == 1
    assert not old.exists() and boundary.exists() and new.exists()


def test_missing_trash_succeeds_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "media-store"
    root.mkdir()
    assert purge_media_trash.build_deletion_plan(root, 1, now=date(2026, 9, 13)) == []
    assert purge_media_trash.execute_deletion_plan([], root) == 0
    assert not (root / "trash").exists()


@pytest.mark.parametrize(
    "bad_root",
    [Path("relative-media-store"), Path("relative") / "media-store"],
)
def test_non_absolute_root_is_rejected(bad_root: Path) -> None:
    with pytest.raises(purge_media_trash.PurgeError):
        purge_media_trash.build_deletion_plan(bad_root, 1)


@pytest.mark.parametrize("argument", ["0", "-1", "+1", "abc"])
def test_cli_requires_positive_integer_days(argument: str) -> None:
    with pytest.raises(SystemExit):
        purge_media_trash.main(["--older-than-days", argument])


def test_invalid_date_or_unknown_source_is_plan_first_and_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "media-store"
    root.mkdir()
    candidate = _file(root, "2026-09-01")
    invalid = root / "trash" / "2026-02-30" / "public" / "lineup" / "bad.mp4"
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b"bad")
    with pytest.raises(purge_media_trash.PurgeError):
        purge_media_trash.build_deletion_plan(root, 1, now=date(2026, 9, 13))
    assert candidate.exists()

    invalid.unlink()
    unknown = root / "trash" / "2026-09-01" / "unknown" / "lineup" / "bad.mp4"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"bad")
    with pytest.raises(purge_media_trash.PurgeError):
        purge_media_trash.build_deletion_plan(root, 1, now=date(2026, 9, 13))
    assert candidate.exists()


def test_symlink_is_rejected_without_deleting_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "media-store"
    root.mkdir()
    candidate = _file(root, "2026-09-01")
    link = root / "trash" / "2026-09-01" / "public" / "lineup" / "link.mp4"
    try:
        link.symlink_to(candidate)
    except OSError:
        # Windows developer mode may be unavailable; simulate the OS reparse
        # classification at the same boundary without skipping the test.
        link.write_bytes(b"link placeholder")
        original = purge_media_trash._is_reparse_or_symlink
        monkeypatch.setattr(
            purge_media_trash,
            "_is_reparse_or_symlink",
            lambda path: path == link or original(path),
        )
    with pytest.raises(purge_media_trash.PurgeError):
        purge_media_trash.build_deletion_plan(root, 1, now=date(2026, 9, 13))
    assert candidate.exists()


def test_unknown_object_and_invalid_bucket_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "media-store"
    root.mkdir()
    invalid_bucket = root / "trash" / "not-a-date"
    invalid_bucket.mkdir(parents=True)
    with pytest.raises(purge_media_trash.PurgeError):
        purge_media_trash.build_deletion_plan(root, 1)
    invalid_bucket.rmdir()
    (root / "trash").mkdir(exist_ok=True)
    (root / "trash" / "2026-09-01").write_bytes(b"not-a-dir")
    with pytest.raises(purge_media_trash.PurgeError):
        purge_media_trash.build_deletion_plan(root, 1)
