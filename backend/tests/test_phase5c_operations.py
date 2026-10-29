from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.scripts import backup, restore, run_evals


def _smoke_load_module():
    path = Path(__file__).parents[2] / "scripts" / "smoke_load.py"
    spec = importlib.util.spec_from_file_location("smokeline_smoke_load", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_group(
    tmp_path: Path,
    name: str,
    *,
    include_checksums: bool = True,
    media_mode: int | None = None,
) -> Path:
    group = tmp_path / name
    (group / "db").mkdir(parents=True)
    (group / "public" / "lineup").mkdir(parents=True)
    (group / "archive").mkdir()
    (group / "db" / f"smokeline-{name}.dump").write_bytes(b"custom dump")
    public = group / "public" / "lineup" / "video.mp4"
    public.write_bytes(b"media")
    if media_mode is not None:
        os.chmod(public, media_mode)
    media = group / "media.tar"
    with tarfile.open(media, "w") as archive:
        archive.add(public, arcname="public/lineup/video.mp4", recursive=False)
    files = [
        {
            "path": "public/lineup/video.mp4",
            "size_bytes": public.stat().st_size,
            "sha256": backup._sha256(public),
        }
    ]
    # The test group uses a sibling staging tree matching the backup layout;
    # _write_manifest only needs the database and media artifacts in `group`.
    backup._write_manifest(group, backup._utc_now(), files)
    if not include_checksums:
        (group / "checksums.sha256").unlink()
    return group


def test_jsonl_examples_are_strict_and_cover_five_cases(tmp_path: Path) -> None:
    path = Path(__file__).parents[2] / "evals" / "query_cases.example.jsonl"
    cases = run_evals.load_cases(path)
    assert len(cases) == 5
    assert {"target_only", "origin_only", "catalog", "exact"}.issubset(
        {tag for case in cases for tag in case.tags}
    )
    invalid = tmp_path / "phase5c-invalid.jsonl"
    invalid.write_text(
        json.dumps(
            {
                "id": "Q999",
                "turns": [{"kind": "query", "text": "烟", "input_mode": "text", "extra": 1}],
                "expected": {
                    "response_type": "route_matches",
                    "target_slug": None,
                    "origin_slug": None,
                    "selected_lineup_id": None,
                    "routes": [],
                    "clarification_field": None,
                    "not_found_reason": None,
                    "llm_calls_max": 0,
                },
                "tags": ["invalid"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(ValueError):
            run_evals.load_cases(invalid)
    finally:
        invalid.unlink()


def test_evaluation_tags_schema_matches_runner_limits(tmp_path: Path) -> None:
    schema = json.loads(
        (Path(__file__).parents[2] / "evals" / "query_cases.schema.json").read_text(
            encoding="utf-8"
        )
    )
    tags_schema = schema["properties"]["tags"]
    assert tags_schema["minItems"] == 1
    assert tags_schema["maxItems"] == 32
    assert tags_schema["items"] == {"type": "string", "minLength": 1, "maxLength": 64}

    sample = json.loads(
        Path(__file__)
        .parents[2]
        .joinpath("evals", "query_cases.example.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    sample["tags"] = ["t" * 64] * 32
    valid = tmp_path / "valid-tags.jsonl"
    valid.write_text(json.dumps(sample) + "\n", encoding="utf-8")
    assert len(run_evals.load_cases(valid)[0].tags) == 32

    for tags in ([], ["t" * 65], ["t"] * 33):
        invalid = tmp_path / "invalid-tags.jsonl"
        sample["tags"] = tags
        invalid.write_text(json.dumps(sample) + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            run_evals.load_cases(invalid)


def test_evaluation_comparison_checks_routes_and_match_ids() -> None:
    case = run_evals.load_cases(Path(__file__).parents[2] / "evals" / "query_cases.example.jsonl")[
        1
    ]
    good = {
        "type": "route_matches",
        "intent": {"target_slug": "ct", "origin_slug": None},
        "routes": [
            {
                "target": {"slug": "ct"},
                "origin": {"slug": "a-ramp"},
                "lineup": {"id": "11111111-1111-4111-8111-111111111111"},
            },
            {
                "target": {"slug": "ct"},
                "origin": {"slug": "t-spawn"},
                "lineup": {"id": "22222222-2222-4222-8222-222222222222"},
            },
        ],
    }
    assert run_evals._compare(case, good) is None
    assert run_evals._compare(case, {"type": "unknown"}) == "RESPONSE_SHAPE"
    bad = {**good, "routes": list(reversed(good["routes"]))}
    assert run_evals._compare(case, bad) == "ROUTES_MISMATCH"
    unknown = run_evals.load_cases(
        Path(__file__).parents[2] / "evals" / "query_cases.example.jsonl"
    )[-1]
    assert unknown.turns[0].kind == "query"
    assert (
        run_evals._compare(
            unknown,
            {"type": "not_found", "reason": "NO_TARGET"},
        )
        is None
    )
    assert (
        run_evals._compare(
            unknown,
            {"type": "not_found", "reason": "NO_LOCAL_RESULT"},
        )
        == "NOT_FOUND_REASON_MISMATCH"
    )

    no_local_value = json.loads(
        Path(__file__)
        .parents[2]
        .joinpath("evals", "query_cases.example.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    no_local_value["id"] = "Q006"
    no_local_value["expected"].update(
        {"not_found_reason": "NO_LOCAL_RESULT", "target_slug": None, "origin_slug": None}
    )
    no_local = run_evals.EvaluationCase.model_validate(no_local_value)
    assert run_evals._compare(no_local, {"type": "not_found", "reason": "NO_LOCAL_RESULT"}) is None
    assert (
        run_evals._compare(no_local, {"type": "not_found", "reason": "NO_TARGET"})
        == "NOT_FOUND_REASON_MISMATCH"
    )

    clarification_value = json.loads(
        Path(__file__)
        .parents[2]
        .joinpath("evals", "query_cases.example.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    clarification_value["id"] = "Q007"
    clarification_value["expected"].update(
        {
            "response_type": "clarification",
            "target_slug": None,
            "origin_slug": None,
            "selected_lineup_id": None,
            "routes": [],
            "clarification_field": "target",
            "not_found_reason": None,
        }
    )
    clarification = run_evals.EvaluationCase.model_validate(clarification_value)
    assert (
        run_evals._compare(
            clarification,
            {"type": "clarification", "field": "target"},
        )
        is None
    )


def test_backup_manifest_empty_corrupt_and_partial_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(restore.RestoreError, match="RESTORE_MANIFEST_INVALID"):
        restore.validate_backup_group(empty)

    group = _make_group(tmp_path, "20260910-010101")
    assert restore.validate_backup_group(group)["format_version"] == 1
    (group / "media.tar").write_bytes(b"corrupt")
    with pytest.raises(restore.RestoreError):
        restore.validate_backup_group(group)

    partial = _make_group(tmp_path, "20260910-010102", include_checksums=False)
    with pytest.raises(restore.RestoreError, match="RESTORE_CHECKSUM_LIST_INVALID"):
        restore.validate_backup_group(partial)


def test_restore_scope_and_media_extraction_are_isolated(tmp_path: Path, monkeypatch) -> None:
    group = _make_group(tmp_path, "20260910-010103")
    restore_root = tmp_path / "isolated"
    media_root = restore_root / "media"
    compose = restore_root / "compose.restore.yml"
    compose.parent.mkdir()
    compose.write_text(
        """services:
  nginx:
    labels:
      com.smokeline.restore.project: '${RESTORE_PROJECT:?required}'
    environment:
      APP_ENV: test
      AI_PROVIDER: disabled
    ports:
      - '127.0.0.1:${RESTORE_PORT:-18080}:8080'
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", "smokeline-restore-test1")
    restore.validate_restore_scope(
        project="smokeline-restore-test1",
        restore_root=restore_root,
        media_root=media_root,
        backup_path=group,
        compose_file=compose,
        port=18080,
    )
    restore.restore_media_archive(group, media_root)
    assert (media_root / "public/lineup/video.mp4").read_bytes() == b"media"
    assert not any(path.name.startswith(".media.restore-") for path in restore_root.iterdir())
    with pytest.raises(restore.RestoreError, match="RESTORE_PRODUCTION_SCOPE_REJECTED"):
        restore.validate_restore_scope(
            project="smokeline",
            restore_root=restore_root,
            media_root=media_root,
            backup_path=group,
            compose_file=compose,
            port=18080,
        )
    with pytest.raises(restore.RestoreError, match="RESTORE_PORT_REJECTED"):
        restore.validate_restore_scope(
            project="smokeline-restore-test1",
            restore_root=restore_root,
            media_root=media_root,
            backup_path=group,
            compose_file=compose,
            port=8080,
        )


def test_restore_scope_rejects_production_paths_and_compose_markers(
    tmp_path: Path, monkeypatch
) -> None:
    group = _make_group(tmp_path, "20260910-010103-scope")
    restore_root = tmp_path / "isolated-scope"
    media_root = restore_root / "media"
    compose = restore_root / "compose.restore.yml"
    compose.parent.mkdir()
    valid_compose = """services:
  backend:
    labels:
      com.smokeline.restore.project: '${RESTORE_PROJECT:?required}'
    environment:
      APP_ENV: test
      AI_PROVIDER: disabled
    ports:
      - '127.0.0.1:${RESTORE_PORT:-18080}:8080'
"""
    compose.write_text(valid_compose, encoding="utf-8")
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", "smokeline-restore-scope")

    invalid_calls = [
        (
            "bad_project",
            dict(project="smokeline-prod", port=18080),
            "RESTORE_PRODUCTION_SCOPE_REJECTED",
        ),
        ("bad_port", dict(project="smokeline-restore-scope", port=443), "RESTORE_PORT_REJECTED"),
        (
            "prod_root",
            dict(project="smokeline-restore-scope", restore_root=tmp_path / "production"),
            "RESTORE_PRODUCTION_PATH_REJECTED",
        ),
        (
            "media_escape",
            dict(project="smokeline-restore-scope", media_root=tmp_path / "outside"),
            "RESTORE_MEDIA_PATH_REJECTED",
        ),
    ]
    for _label, overrides, expected in invalid_calls:
        kwargs = {
            "project": "smokeline-restore-scope",
            "restore_root": restore_root,
            "media_root": media_root,
            "backup_path": group,
            "compose_file": compose,
            "port": 18080,
        }
        kwargs.update(overrides)
        with pytest.raises(restore.RestoreError, match=expected):
            restore.validate_restore_scope(**kwargs)

    for content, expected in (
        (
            valid_compose.replace("APP_ENV: test", "APP_ENV: prod"),
            "RESTORE_NONPRODUCTION_ENV_REJECTED",
        ),
        (
            valid_compose.replace("AI_PROVIDER: disabled", "AI_PROVIDER: real"),
            "RESTORE_NONPRODUCTION_ENV_REJECTED",
        ),
        (
            valid_compose.replace("com.smokeline.restore.project", "com.other.project"),
            "RESTORE_ISOLATION_MARKER_REJECTED",
        ),
        (
            valid_compose.replace("${RESTORE_PROJECT", "${OTHER_PROJECT"),
            "RESTORE_ISOLATION_MARKER_REJECTED",
        ),
        (valid_compose.replace("18080", "19000"), "RESTORE_PORT_NOT_DECLARED"),
        (valid_compose + "  # compose.prod\n", "RESTORE_PRODUCTION_COMPOSE_REJECTED"),
    ):
        compose.write_text(content, encoding="utf-8")
        with pytest.raises(restore.RestoreError, match=expected):
            restore.validate_restore_scope(
                project="smokeline-restore-scope",
                restore_root=restore_root,
                media_root=media_root,
                backup_path=group,
                compose_file=compose,
                port=18080,
            )
    compose.write_text(valid_compose, encoding="utf-8")


def test_restore_scope_port_token_boundaries(tmp_path: Path, monkeypatch) -> None:
    group = _make_group(tmp_path, "20260910-010103-port-boundary")
    restore_root = tmp_path / "isolated-port-boundary"
    media_root = restore_root / "media"
    compose = restore_root / "compose.restore.yml"
    compose.parent.mkdir()
    template = """services:
  backend:
    labels:
      com.smokeline.restore.project: '${RESTORE_PROJECT:?required}'
    environment:
      APP_ENV: test
      AI_PROVIDER: disabled
    ports:
      - {mapping}
"""
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", "smokeline-restore-port-boundary")

    for mapping in (
        "127.0.0.1:18080:8080",
        "18080:8080/tcp",
        "'[::1]:18080:8080/tcp'",
    ):
        compose.write_text(template.replace("{mapping}", mapping), encoding="utf-8")
        restore.validate_restore_scope(
            project="smokeline-restore-port-boundary",
            restore_root=restore_root,
            media_root=media_root,
            backup_path=group,
            compose_file=compose,
            port=18080,
        )

    for mapping in (
        "80:8080",
        "'80:8080'",
        "127.0.0.1:80:8080",
        "'127.0.0.1:80:8080'",
        "127.0.0.1:80:8080/tcp",
        "'[::1]:80:8080'",
        "'[::1]:443:8443/udp'",
    ):
        compose.write_text(template.replace("{mapping}", mapping), encoding="utf-8")
        with pytest.raises(restore.RestoreError, match="RESTORE_PRODUCTION_COMPOSE_REJECTED"):
            restore.validate_restore_scope(
                project="smokeline-restore-port-boundary",
                restore_root=restore_root,
                media_root=media_root,
                backup_path=group,
                compose_file=compose,
                port=18080,
            )


def test_restore_file_and_destination_guards(monkeypatch, tmp_path: Path) -> None:
    group = _make_group(tmp_path, "20260910-010103-files")
    with pytest.raises(restore.RestoreError, match="restore checksum failed"):
        restore._sha256(tmp_path / "missing")
    with pytest.raises(restore.RestoreError, match="RESTORE_BACKUP_PATH_REJECTED"):
        restore.validate_backup_group(tmp_path / "missing")
    relative = Path("relative-backup")
    with pytest.raises(restore.RestoreError, match="RESTORE_BACKUP_PATH_REJECTED"):
        restore.validate_backup_group(relative)

    destination = tmp_path / "not-empty"
    (destination / "existing").mkdir(parents=True)
    with pytest.raises(restore.RestoreError, match="RESTORE_MEDIA_DESTINATION_NOT_EMPTY"):
        restore.restore_media_archive(group, destination)
    destination_file = tmp_path / "destination-file"
    destination_file.write_bytes(b"not a directory")
    with pytest.raises(restore.RestoreError, match="RESTORE_MEDIA_PATH_REJECTED"):
        restore.restore_media_archive(group, destination_file)
    try:
        destination = tmp_path / "destination-link"
        destination.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(restore.RestoreError, match="RESTORE_MEDIA_PATH_REJECTED"):
        restore.restore_media_archive(group, destination)

    with pytest.raises(restore.RestoreError, match="RESTORE_DATABASE_SCOPE_REJECTED"):
        restore.restore_database(group, "postgresql+psycopg://smokeline:secret@postgres:5432/other")

    def fail_restore(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "pg_restore")

    monkeypatch.setattr(restore.subprocess, "run", fail_restore)
    with pytest.raises(restore.RestoreError, match="pg_restore failed"):
        restore.restore_database(
            group, "postgresql+psycopg://smokeline:secret@postgres:5432/smokeline"
        )


def test_restore_media_archive_keeps_existing_mountpoint(monkeypatch, tmp_path: Path) -> None:
    group = _make_group(tmp_path, "20260910-010103-mountpoint", media_mode=0o600)
    destination = tmp_path / "mounted-media"
    destination.mkdir()
    original_rmdir = Path.rmdir

    def reject_mountpoint_rmdir(path: Path) -> None:
        if path == destination:
            raise OSError("mountpoint cannot be removed")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", reject_mountpoint_rmdir)
    restore.restore_media_archive(group, destination)

    assert destination.is_dir()
    assert (destination / "public" / "lineup" / "video.mp4").read_bytes() == b"media"
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o755
        assert destination.joinpath("public").stat().st_mode & 0o777 == 0o755
        assert destination.joinpath("public", "lineup").stat().st_mode & 0o777 == 0o755
        assert destination.joinpath("public", "lineup", "video.mp4").stat().st_mode & 0o777 == 0o644
    assert not any(path.name.startswith(".restore-") for path in destination.iterdir())


def test_restore_media_archive_rolls_back_top_level_commit(monkeypatch, tmp_path: Path) -> None:
    group = _make_group(tmp_path, "20260910-010103-rollback", media_mode=0o600)
    archived = group / "archive" / "old.webm"
    archived.write_bytes(b"archived")
    os.chmod(archived, 0o600)
    public = group / "public" / "lineup" / "video.mp4"
    media = group / "media.tar"
    with tarfile.open(media, "w") as archive:
        archive.add(public, arcname="public/lineup/video.mp4", recursive=False)
        archive.add(archived, arcname="archive/old.webm", recursive=False)
    backup._write_manifest(
        group,
        backup._utc_now(),
        [
            {
                "path": "public/lineup/video.mp4",
                "size_bytes": public.stat().st_size,
                "sha256": backup._sha256(public),
            },
            {
                "path": "archive/old.webm",
                "size_bytes": archived.stat().st_size,
                "sha256": backup._sha256(archived),
            },
        ],
    )
    destination = tmp_path / "rollback-media"
    destination.mkdir()
    original_replace = os.replace
    committed = 0
    observed_archive_modes: tuple[int, int] | None = None

    def fail_second_top_level_replace(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ):
        nonlocal committed, observed_archive_modes
        target_path = Path(target)
        if target_path.parent == destination:
            committed += 1
            if committed == 2:
                raise OSError("simulated second top-level commit failure")
        result = original_replace(source, target)
        if target_path.parent == destination and committed == 1:
            archive_dir = target_path / "old.webm"
            observed_archive_modes = (
                target_path.stat().st_mode & 0o777,
                archive_dir.stat().st_mode & 0o777,
            )
        return result

    monkeypatch.setattr(restore.os, "replace", fail_second_top_level_replace)
    with pytest.raises(restore.RestoreError, match="^RESTORE_MEDIA_EXTRACTION_FAILED$"):
        restore.restore_media_archive(group, destination)

    assert committed == 2
    if os.name == "posix":
        assert observed_archive_modes == (0o700, 0o640)
    assert list(destination.iterdir()) == []


def test_retention_keeps_seven_complete_groups_and_ignores_partial(tmp_path: Path) -> None:
    groups = [_make_group(tmp_path, f"20260910-0101{index:02d}") for index in range(8)]
    partial = _make_group(tmp_path, "20260910-019999", include_checksums=False)
    for index, group in enumerate(groups):
        stamp = 1_700_000_000 + index
        os.utime(group, (stamp, stamp))
    retained = backup.retain_backups(tmp_path)
    assert len(retained) == 7
    assert partial.exists()
    assert len([path for path in tmp_path.iterdir() if backup._complete_group(path)]) == 7


def test_pg_dump_uses_custom_format_and_does_not_log_password(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "db" / "dump"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(b"dump")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    backup._pg_dump(SimpleNamespace(postgres_password_value="secret-not-output"), destination)
    assert "--format=custom" in calls[0]
    assert destination.read_bytes() == b"dump"


def test_request_pacing_applies_across_case_boundaries(monkeypatch) -> None:
    clock = iter([10.0, 11.0, 13.3])
    sleeps: list[float] = []
    monkeypatch.setattr(run_evals.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(run_evals.time, "sleep", sleeps.append)
    first = run_evals._pace_request(None, 2.2)
    second = run_evals._pace_request(first, 2.2)
    assert first == 10.0
    assert sleeps == pytest.approx([1.2])
    assert second == 13.3


def test_selection_jsonl_is_flat_and_runner_injects_nested_conversation(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "selection.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "Q006",
                "turns": [
                    {"kind": "query", "text": "警家烟怎么丢", "input_mode": "text"},
                    {"kind": "selection", "field": "target", "value": "ct"},
                ],
                "expected": {
                    "response_type": "match",
                    "target_slug": "ct",
                    "origin_slug": "a-ramp",
                    "selected_lineup_id": "11111111-1111-4111-8111-111111111111",
                    "routes": [],
                    "clarification_field": None,
                    "not_found_reason": None,
                    "llm_calls_max": 0,
                },
                "tags": ["selection"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    case = run_evals.load_cases(path)[0]
    assert case.turns[1].value == "ct"
    payloads: list[dict[str, object]] = []

    def fake_post(_client, _base_url, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return {"type": "route_matches", "conversation_id": "conv"}, "", "a" * 32
        return (
            {
                "type": "match",
                "conversation_id": "conv",
                "intent": {"target_slug": "ct", "origin_slug": "a-ramp"},
                "lineup": {"id": "11111111-1111-4111-8111-111111111111"},
            },
            "",
            "b" * 32,
        )

    monkeypatch.setattr(run_evals, "_post_search", fake_post)

    async def zero_count(_ids):
        return 0

    monkeypatch.setattr(run_evals, "count_intent_reservations", zero_count)
    monkeypatch.setattr(run_evals.time, "sleep", lambda _seconds: None)
    results = run_evals.run_cases([case], "http://example.test", 2.2)
    assert payloads[1] == {
        "kind": "selection",
        "conversation_id": "conv",
        "selection": {"field": "target", "value": "ct"},
    }
    assert results[0]["status"] == "PASS"
    assert results[0]["selected_lineup_id"] == "11111111-1111-4111-8111-111111111111"
    assert results[0]["routes"] == []
    assert results[0]["clarification_field"] is None
    assert results[0]["not_found_reason"] is None


def test_runner_counts_reservations_after_response_mismatch(monkeypatch) -> None:
    case = run_evals.load_cases(Path(__file__).parents[2] / "evals" / "query_cases.example.jsonl")[
        0
    ]
    seen_ids: list[str] = []

    def fake_post(_client, _base_url, _payload):
        return (
            {
                "type": "match",
                "conversation_id": "conv",
                "intent": {"target_slug": "ct", "origin_slug": "a-ramp"},
                "lineup": {"id": "22222222-2222-4222-8222-222222222222"},
            },
            "",
            "c" * 32,
        )

    async def fake_count(request_ids):
        seen_ids.extend(request_ids)
        return 0

    monkeypatch.setattr(run_evals, "_post_search", fake_post)
    monkeypatch.setattr(run_evals, "count_intent_reservations", fake_count)
    results = run_evals.run_cases([case], "http://example.test", 2.2)
    assert results[0]["status"] == "FAIL"
    assert results[0]["error"] == "LINEUP_MISMATCH"
    assert seen_ids == ["c" * 32]
    assert results[0]["llm_calls"] == 0


def test_evaluation_input_and_http_error_shapes(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        run_evals._base_url("not-a-url")
    with pytest.raises(ValueError, match="forbidden"):
        run_evals._base_url("https://user:pass@example.test/path")
    with pytest.raises(ValueError, match="forbidden"):
        run_evals._base_url("https://example.test/path?secret=1")
    with pytest.raises(ValueError, match="forbidden"):
        run_evals._base_url("https://example.test/api")
    assert run_evals._base_url("https://example.test/") == "https://example.test"
    assert run_evals._request_id(httpx.Headers({"x-request-id": "a" * 32})) == "a" * 32
    assert run_evals._request_id(httpx.Headers({"x-request-id": "bad"})) == ""

    assert run_evals._intent_from_response({}) == (None, None)
    assert run_evals._intent_from_response({"intent": {"target_slug": 1, "origin_slug": "x"}}) == (
        None,
        "x",
    )
    assert run_evals._routes_from_response({"routes": ["bad"]}) is None
    assert (
        run_evals._routes_from_response({"routes": [{"target": {}, "origin": {}, "lineup": {}}]})
        is None
    )
    assert run_evals._routes_from_response({"routes": [{"target": {"slug": 1}}]}) is None

    class FailingClient:
        def __init__(self):
            self.headers = None

        def post(self, *_args, **_kwargs):
            self.headers = _kwargs["headers"]
            raise httpx.ReadTimeout("timeout")

    failing_client = FailingClient()
    body, error, request_id = run_evals._post_search(failing_client, "https://example.test", {})
    assert body is None and error == "HTTP_REQUEST_FAILED"
    assert request_id == failing_client.headers["X-Request-ID"]
    assert len(request_id) == 32 and all(char in "0123456789abcdef" for char in request_id)

    class ResponseClient:
        def __init__(self, response):
            self.response = response

        def post(self, *_args, **_kwargs):
            return self.response

    request = httpx.Request("POST", "https://example.test/api/v1/search")
    response = httpx.Response(503, request=request, headers={"x-request-id": "b" * 32})
    body, error, request_id = run_evals._post_search(
        ResponseClient(response), "https://example.test", {}
    )
    assert body is None and error == "HTTP_503" and request_id == "b" * 32
    invalid_json = httpx.Response(200, request=request, content=b"not-json")
    assert (
        run_evals._post_search(ResponseClient(invalid_json), "https://example.test", {})[1]
        == "INVALID_JSON"
    )
    wrong_shape = httpx.Response(200, request=request, json=[1])
    assert (
        run_evals._post_search(ResponseClient(wrong_shape), "https://example.test", {})[1]
        == "RESPONSE_SHAPE"
    )

    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        run_evals.load_cases(blank)
    duplicate = tmp_path / "duplicate.jsonl"
    sample = Path(__file__).parents[2] / "evals" / "query_cases.example.jsonl"
    first = sample.read_text(encoding="utf-8").splitlines()[0]
    duplicate.write_text(first + "\n" + first + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        run_evals.load_cases(duplicate)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        run_evals.load_cases(empty)

    with pytest.raises(ValueError, match="interval"):
        run_evals.run_cases([], "https://example.test", 2.1)


def test_evaluation_semantics_reject_impossible_expectations() -> None:
    sample = json.loads(
        Path(__file__)
        .parents[2]
        .joinpath("evals", "query_cases.example.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    cases = [
        (lambda value: value["expected"].update({"selected_lineup_id": None}), "match expectation"),
        (
            lambda value: value["expected"].update(
                {"response_type": "route_matches", "routes": []}
            ),
            "route_matches expectation",
        ),
        (
            lambda value: value["expected"].update(
                {
                    "response_type": "clarification",
                    "target_slug": None,
                    "origin_slug": None,
                    "selected_lineup_id": None,
                    "clarification_field": None,
                }
            ),
            "clarification must",
        ),
        (
            lambda value: value["expected"].update(
                {
                    "response_type": "not_found",
                    "target_slug": None,
                    "origin_slug": None,
                    "selected_lineup_id": None,
                    "not_found_reason": None,
                }
            ),
            "not_found expectation",
        ),
        (
            lambda value: value["expected"].update(
                {"response_type": "match", "not_found_reason": "NO_TARGET"}
            ),
            "not_found reason",
        ),
        (
            lambda value: value["expected"].update(
                {"response_type": "match", "clarification_field": "target"}
            ),
            "clarification field",
        ),
    ]
    for mutate, error in cases:
        value = json.loads(json.dumps(sample))
        mutate(value)
        with pytest.raises(ValueError, match=error):
            run_evals._validate_case_semantics(run_evals.EvaluationCase.model_validate(value))
    no_query = json.loads(json.dumps(sample))
    no_query["turns"] = [{"kind": "selection", "field": "target", "value": "ct"}]
    with pytest.raises(ValueError, match="must contain a query"):
        run_evals._validate_case_semantics(run_evals.EvaluationCase.model_validate(no_query))
    non_route_fields = json.loads(json.dumps(sample))
    non_route_fields["expected"].update(
        {"response_type": "clarification", "clarification_field": "target"}
    )
    with pytest.raises(ValueError, match="non-route expectation"):
        run_evals._validate_case_semantics(
            run_evals.EvaluationCase.model_validate(non_route_fields)
        )
    non_route_intent = json.loads(json.dumps(sample))
    non_route_intent["expected"].update(
        {
            "response_type": "not_found",
            "not_found_reason": "NO_LOCAL_RESULT",
            "target_slug": "ct",
            "selected_lineup_id": None,
        }
    )
    with pytest.raises(ValueError, match="non-route intent fields"):
        run_evals._validate_case_semantics(
            run_evals.EvaluationCase.model_validate(non_route_intent)
        )


def test_restore_manifest_rejects_duplicate_checksum_and_cleans_failed_extract(
    tmp_path: Path,
) -> None:
    group = _make_group(tmp_path, "20260910-010104")
    checksums = (group / "checksums.sha256").read_text(encoding="ascii")
    (group / "checksums.sha256").write_text(
        checksums + checksums.splitlines()[0] + "\n", encoding="ascii"
    )
    with pytest.raises(restore.RestoreError, match="RESTORE_CHECKSUM_LIST_INVALID"):
        restore.validate_backup_group(group)

    group = _make_group(tmp_path, "20260910-010105")
    destination = tmp_path / "extract"
    original_open = restore.tarfile.open
    open_count = 0

    def fail_after_one_file(path, mode):
        nonlocal open_count
        archive = original_open(path, mode)
        if Path(path) != group / "media.tar":
            return archive
        open_count += 1
        if open_count == 1:
            return archive

        class _Archive:
            def __enter__(self):
                archive.__enter__()
                return self

            def __exit__(self, *args):
                return archive.__exit__(*args)

            def getmembers(self):
                return archive.getmembers()

            def extractfile(self, member):
                source = archive.extractfile(member)
                assert source is not None
                first_read = True

                class _FailingReader:
                    def read(self, _size=-1):
                        nonlocal first_read
                        if first_read:
                            first_read = False
                            return source.read(1)
                        raise OSError("injected extraction failure")

                return _FailingReader()

        return _Archive()

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(restore.tarfile, "open", fail_after_one_file)
        with pytest.raises(restore.RestoreError, match="^RESTORE_MEDIA_EXTRACTION_FAILED$"):
            restore.restore_media_archive(group, destination)
    finally:
        monkeypatch.undo()
    assert not destination.exists()

    for symlink_name, target in (
        ("manifest.json", "checksums.sha256"),
        ("checksums.sha256", "manifest.json"),
    ):
        symlink_group = _make_group(tmp_path, f"20260910-010105-{symlink_name[:2]}")
        link = symlink_group / symlink_name
        link.unlink()
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(restore.RestoreError, match="RESTORE_MANIFEST_INVALID"):
            restore.validate_backup_group(symlink_group)


def test_backup_walks_media_trash_and_space_guards(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "media"
    (root / "public" / "nested").mkdir(parents=True)
    (root / "archive").mkdir()
    public = root / "public" / "nested" / "video.mp4"
    public.write_bytes(b"video")
    files = backup._iter_media_files(root, "public")
    assert files == [("nested/video.mp4", public)]
    with pytest.raises(backup.BackupError, match="escaped"):
        backup._safe_relative(root, tmp_path / "outside")
    stale_time = (backup._utc_now() - backup.STALE_AGE - timedelta(minutes=1)).timestamp()
    os.utime(public, (stale_time, stale_time))
    assert backup._move_stale_media(root, "public", files, set(), backup._utc_now()) == 1
    trashed = list((root / "trash").rglob("video.mp4"))
    assert len(trashed) == 1
    staging_file = root / ".staging" / "leftover.part"
    staging_file.parent.mkdir()
    staging_file.write_bytes(b"staging")
    os.utime(staging_file, (stale_time, stale_time))
    assert backup._move_stale_staging(root, backup._utc_now()) == 1
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(backup.BackupError, match="BACKUP_INSUFFICIENT_SPACE"):
        backup._check_space(root, 1)
    with pytest.raises(backup.BackupError, match="media size unavailable"):
        backup._media_size_bytes(root, [("missing", root / "missing")], [])


def test_backup_root_is_private_and_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    backup._ensure_private_directory(root, error="unsafe backup root")
    assert root.is_dir()
    if os.name == "posix":
        assert root.stat().st_mode & 0o777 == 0o700
    file_path = tmp_path / "not-a-directory"
    file_path.write_bytes(b"x")
    with pytest.raises(backup.BackupError, match="unsafe backup root"):
        backup._ensure_private_directory(file_path, error="unsafe backup root")
    if hasattr(os, "symlink"):
        link = tmp_path / "backup-link"
        try:
            link.symlink_to(root, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(backup.BackupError, match="unsafe backup root"):
            backup._ensure_private_directory(link, error="unsafe backup root")


def test_backup_low_level_guards_reject_changed_files(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(backup.BackupError, match="checksum failed"):
        backup._sha256(missing)
    with pytest.raises(backup.BackupError, match="fsync failed"):
        backup._fsync_file(missing)
    if os.name == "posix":
        with pytest.raises(backup.BackupError, match="directory fsync failed"):
            backup._fsync_directory(missing)
    else:
        backup._fsync_directory(missing)

    destination = tmp_path / "destination"
    destination.write_bytes(b"old")
    target = tmp_path / "target"
    try:
        target.symlink_to(destination)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(backup.BackupError, match="destination is a symlink"):
        backup._write_atomic(target, b"new")

    media_root = tmp_path / "media"
    (media_root / "public").mkdir(parents=True)
    (media_root / "archive").mkdir()
    public_link = media_root / "public-link"
    public_link.symlink_to(media_root / "public", target_is_directory=True)
    with pytest.raises(backup.BackupError, match="symlinked directory"):
        backup._iter_media_files(media_root, "public-link")
    file_link = media_root / "public" / "file.mp4"
    file_link.symlink_to(destination)
    with pytest.raises(backup.BackupError, match="non-regular file"):
        backup._iter_media_files(media_root, "public")

    trash_link = media_root / "trash"
    trash_link.symlink_to(destination)
    with pytest.raises(backup.BackupError, match="unsafe trash root"):
        backup._trash_path(media_root, "public", "file.mp4", backup._utc_now())
    staging_link = media_root / ".staging"
    staging_link.symlink_to(destination)
    with pytest.raises(backup.BackupError, match="unsafe staging root"):
        backup._move_stale_staging(media_root, backup._utc_now())

    def fail_dump(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "pg_dump")

    monkeypatch.setattr(backup.subprocess, "run", fail_dump)
    with pytest.raises(backup.BackupError, match="pg_dump failed"):
        backup._pg_dump(
            SimpleNamespace(postgres_password_value="not-output"), tmp_path / "db" / "dump"
        )

    changed = tmp_path / "changed.mp4"
    changed.symlink_to(destination)
    with pytest.raises(backup.BackupError, match="media changed during archive"):
        backup._tar_media(media_root, tmp_path / "media.tar", [("changed.mp4", changed)], [])


def test_backup_tar_and_manifest_are_tar_member_based(tmp_path: Path) -> None:
    root = tmp_path / "media"
    public = root / "public" / "video.mp4"
    archived = root / "archive" / "old.webP"
    public.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    public.write_bytes(b"public")
    archived.write_bytes(b"archive")
    group = tmp_path / ".20260910-010106.incomplete"
    (group / "db").mkdir(parents=True)
    (group / "db" / "smokeline-20260910-010106.dump").write_bytes(b"dump")
    media = group / "media.tar"
    files = backup._tar_media(root, media, [("video.mp4", public)], [("old.webP", archived)])
    backup._write_manifest(group, backup._utc_now(), files)
    assert [item["path"] for item in files] == ["public/video.mp4", "archive/old.webP"]
    (group / "public").mkdir()
    assert restore.validate_backup_group(group)
    if os.name == "posix":
        assert (media.stat().st_mode & 0o777) == 0o600
        assert (group / "manifest.json").stat().st_mode & 0o777 == 0o600
        assert (group / "checksums.sha256").stat().st_mode & 0o777 == 0o600


def test_create_backup_holds_lock_through_latest_and_preserves_old_pointer(
    monkeypatch, tmp_path: Path
) -> None:
    backup_root = tmp_path / "backups"
    media_root = tmp_path / "media"
    (media_root / "public").mkdir(parents=True)
    (media_root / "archive").mkdir()
    backup_root.mkdir()
    old_latest = backup_root / "latest"
    old_latest.write_text("old-group\n", encoding="ascii")
    settings = SimpleNamespace(backup_root=str(backup_root), media_store_root=str(media_root))
    monkeypatch.setattr(backup, "get_settings", lambda: settings)
    events: list[str] = []

    class Lock:
        async def __aenter__(self):
            events.append("lock-enter")
            return self

        async def __aexit__(self, *_args):
            events.append("lock-exit")

    monkeypatch.setattr(backup, "media_lock", lambda timeout_seconds: Lock())

    async def fake_reconcile():
        events.append("reconcile")

    async def fake_cleanup(_now):
        events.append("cleanup")
        return {}

    monkeypatch.setattr(backup, "reconcile_media", fake_reconcile)
    monkeypatch.setattr(backup, "cleanup_before_dump", fake_cleanup)
    monkeypatch.setattr(backup, "_database_size_bytes", lambda: _async_value(0))
    monkeypatch.setattr(backup, "_iter_media_files", lambda _root, _name: [])
    monkeypatch.setattr(backup, "_media_size_bytes", lambda *_args: 0)
    monkeypatch.setattr(backup, "_check_space", lambda *_args: None)

    def fake_dump(_settings, destination):
        events.append("dump")
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"dump")

    def fake_tar(_root, destination, _files, _archive_files):
        events.append("tar")
        with tarfile.open(destination, "w"):
            pass
        return []

    complete_calls: list[Path] = []
    monkeypatch.setattr(backup, "_pg_dump", fake_dump)
    monkeypatch.setattr(backup, "_tar_media", fake_tar)
    monkeypatch.setattr(backup, "_complete_group", lambda path: complete_calls.append(path) or True)
    monkeypatch.setattr(backup, "retain_backups", lambda *_args, **_kwargs: [])
    now = backup._utc_now()
    result = asyncio_run(backup.create_backup(now=now))
    assert result.is_dir()
    assert (backup_root / "latest").read_text(encoding="ascii") == result.name + "\n"
    assert events[:4] == ["lock-enter", "reconcile", "cleanup", "dump"]
    assert events[-1] == "lock-exit"
    assert complete_calls == [result]

    old_latest.write_text("stable\n", encoding="ascii")
    monkeypatch.setattr(
        backup, "_tar_media", lambda *_args: (_ for _ in ()).throw(backup.BackupError("tar failed"))
    )
    with pytest.raises(backup.BackupError):
        asyncio_run(backup.create_backup(now=now + timedelta(seconds=1)))
    assert old_latest.read_text(encoding="ascii") == "stable\n"

    def valid_tar(_root, destination, _files, _archive_files):
        events.append("tar-valid")
        with tarfile.open(destination, "w"):
            pass
        return []

    monkeypatch.setattr(backup, "_tar_media", valid_tar)
    monkeypatch.setattr(backup, "_complete_group", lambda _path: False)
    failed_group = backup._timestamp(now + timedelta(seconds=2))
    with pytest.raises(backup.BackupError, match="self-validation"):
        asyncio_run(backup.create_backup(now=now + timedelta(seconds=2)))
    assert not (backup_root / failed_group).exists()
    assert old_latest.read_text(encoding="ascii") == "stable\n"


def test_backup_main_uses_stable_structured_error_codes(monkeypatch) -> None:
    records: list[dict[str, object]] = []
    configured: list[bool] = []

    async def fail_backup():
        raise backup.BackupError("BACKUP_INSUFFICIENT_SPACE")

    monkeypatch.setattr(backup, "create_backup", fail_backup)
    monkeypatch.setattr(backup, "configure_logging", lambda: configured.append(True))
    monkeypatch.setattr(
        backup.logger, "error", lambda _message, **kwargs: records.append(kwargs["extra"])
    )
    assert backup.main() == 1
    assert configured == [True]
    assert records == [{"event": "BACKUP_FAILED", "error_code": "BACKUP_INSUFFICIENT_SPACE"}]


def test_restore_requires_strict_manifest_database_and_safe_archive_paths(tmp_path: Path) -> None:
    group = _make_group(tmp_path, "20260910-010107")
    manifest_path = group / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"].pop("format")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(restore.RestoreError, match="RESTORE_MANIFEST_INVALID"):
        restore.validate_backup_group(group)
    with pytest.raises(restore.RestoreError, match="unsafe archive path"):
        restore._safe_member_path(tmp_path, "../outside")
    with pytest.raises(restore.RestoreError, match="unsafe archive path"):
        restore._safe_member_path(tmp_path, "public\\video.mp4")

    group = _make_group(tmp_path, "20260910-010108")
    manifest = json.loads((group / "manifest.json").read_text(encoding="utf-8"))
    manifest["media_files"].append(dict(manifest["media_files"][0]))
    (group / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(restore.RestoreError, match="RESTORE_MANIFEST_INVALID"):
        restore.validate_backup_group(group)


def test_restore_database_scope_and_single_transaction(monkeypatch, tmp_path: Path) -> None:
    group = _make_group(tmp_path, "20260910-010109")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)

    monkeypatch.setattr(restore.subprocess, "run", fake_run)
    restore.restore_database(group, "postgresql+psycopg://smokeline:secret@postgres:5432/smokeline")
    assert "--single-transaction" in calls[0]
    assert str(group / "db" / "smokeline-20260910-010109.dump") == calls[0][-1]
    with pytest.raises(restore.RestoreError, match="RESTORE_DATABASE_SCOPE_REJECTED"):
        restore.restore_database(group, "postgresql+psycopg://smokeline:secret@localhost/smokeline")


def test_restore_scope_fail_closed_and_main_execute_requires_database(
    monkeypatch, tmp_path: Path
) -> None:
    group = _make_group(tmp_path, "20260910-010110")
    restore_root = tmp_path / "restore"
    compose = restore_root / "compose.restore.yml"
    compose.parent.mkdir()
    compose.write_text(
        """services:
  backend:
    labels:
      com.smokeline.restore.project: '${RESTORE_PROJECT:?required}'
    environment:
      APP_ENV: test
      AI_PROVIDER: disabled
    ports:
      - '127.0.0.1:${RESTORE_PORT:-18080}:8080'
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", "smokeline-restore-test2")
    restore.validate_restore_scope(
        project="smokeline-restore-test2",
        restore_root=restore_root,
        media_root=restore_root / "media",
        backup_path=group,
        compose_file=compose,
        port=18080,
    )
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", "different-project")
    with pytest.raises(restore.RestoreError, match="RESTORE_ISOLATION_MARKER_REJECTED"):
        restore.validate_restore_scope(
            project="smokeline-restore-test2",
            restore_root=restore_root,
            media_root=restore_root / "media",
            backup_path=group,
            compose_file=compose,
            port=18080,
        )
    monkeypatch.setenv("SMOKELINE_RESTORE_PROJECT", "smokeline-restore-test2")
    assert (
        restore.main(
            [
                "--backup",
                str(group),
                "--project",
                "smokeline-restore-test2",
                "--restore-root",
                str(restore_root),
                "--media-root",
                str(restore_root / "media"),
                "--compose-file",
                str(compose),
                "--port",
                "18080",
                "--execute",
            ]
        )
        == 2
    )


def _async_value(value):
    async def result():
        return value

    return result()


def asyncio_run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


def test_smoke_load_percentile_and_input_guards() -> None:
    smoke_load = _smoke_load_module()
    assert smoke_load.percentile([1, 2, 3, 4], 0.95) == 4
    with pytest.raises(ValueError):
        smoke_load.run_load("http://unused", 3, 1, 0)


def test_smoke_load_phase5c_warmup_contract_and_pool_bound(monkeypatch) -> None:
    smoke_load = _smoke_load_module()
    calls = []

    def fake_run(base_url, total, concurrency):
        calls.append((base_url, total, concurrency))
        return [
            smoke_load.Sample(
                "/api/v1/maps/mirage" if index % 2 == 0 else "/api/v1/search",
                0.0,
                None,
            )
            for index in range(total)
        ]

    monkeypatch.setattr(smoke_load, "_run_mixed", fake_run)
    smoke_load._warmup("http://unused", 20, 20)
    assert calls == [("http://unused", 20, 20)]
    with pytest.raises(ValueError):
        smoke_load._warmup("http://unused", 10, 20)

    monkeypatch.setenv("DB_POOL_SIZE", "10")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    smoke_load._assert_connection_bound(11)
    with pytest.raises(RuntimeError, match="PERF_POOL_BOUND_EXCEEDED"):
        smoke_load._assert_connection_bound(12)


def test_smoke_load_uses_fixed_workers_and_persistent_clients(monkeypatch) -> None:
    smoke_load = _smoke_load_module()
    clients = []
    barrier = threading.Barrier(20, timeout=5)

    class FakeClient:
        def __init__(self, _base_url):
            self.request_count = 0
            self.closed = 0
            clients.append(self)

        def close(self):
            self.closed += 1

        def request(self, _method, endpoint, _body, _headers):
            self.request_count += 1
            if self.request_count == 1:
                barrier.wait()
            body = b'{"type":"match"}' if endpoint.endswith("/search") else b"{}"
            return 200, body

    monkeypatch.setattr(smoke_load, "_PersistentHTTPClient", FakeClient)
    samples = smoke_load._run_mixed("http://unused", 100, 20)

    assert len(samples) == 100
    assert len(clients) == 20
    assert sum(sample.endpoint == "/api/v1/maps/mirage" for sample in samples) == 50
    assert sum(sample.endpoint == "/api/v1/search" for sample in samples) == 50
    assert sum(client.request_count for client in clients) == 100
    assert max(client.request_count for client in clients) >= 2
    assert all(client.closed == 1 for client in clients)
