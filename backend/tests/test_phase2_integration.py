"""One real PostgreSQL/FFmpeg management-to-public teaching slice.

The test is intentionally opt-in.  Verification containers set
``PHASE2_INTEGRATION_REQUIRED=1``; a developer running the unit suite without
the isolated PostgreSQL service gets an explicit skip instead of a misleading
green integration result.  When enabled, missing or unreachable PostgreSQL is
a hard failure.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.main import app
from app.scripts import reconcile_media

_PASSWORD = "phase2-local-test-password"
_AUTHORIZATION = "Basic " + base64.b64encode(f"admin:{_PASSWORD}".encode()).decode()
_REQUEST_HEADERS = {"Authorization": _AUTHORIZATION}


def _sync_database_url(raw: str) -> str:
    return raw.replace("postgresql+psycopg://", "postgresql://", 1)


def _has_symlink_component(path: Path) -> bool:
    cursor = Path(path.anchor)
    for component in path.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            return True
    return False


def _remove_test_media_tree(candidate: Path, media_root: Path) -> None:
    if not candidate.exists():
        return
    if candidate.is_symlink() or _has_symlink_component(candidate):
        raise AssertionError("test cleanup encountered a symlink")
    root_resolved = media_root.resolve(strict=True)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise AssertionError("test cleanup escaped the isolated media root") from exc
    if candidate_resolved == root_resolved or not candidate.is_dir():
        raise AssertionError("test cleanup target is not a media directory")
    shutil.rmtree(candidate)


@pytest.fixture(scope="module")
def integration_environment() -> Iterator[str]:
    if os.environ.get("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("real Phase 2 integration requires an explicit isolated test stack")
    if os.environ.get("APP_ENV") != "test":
        pytest.fail("real Phase 2 integration must run with APP_ENV=test", pytrace=False)
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.fail("real Phase 2 integration requires DATABASE_URL", pytrace=False)
    try:
        database = make_url(raw_url)
    except (ArgumentError, TypeError, ValueError):
        pytest.fail("real Phase 2 integration database URL is invalid", pytrace=False)
    if (
        database.drivername != "postgresql+psycopg"
        or database.host != "postgres"
        or database.port != 5432
        or database.database != "smokeline"
        or database.username != "smokeline"
    ):
        pytest.fail(
            "real Phase 2 integration requires the isolated postgres test database", pytrace=False
        )
    try:
        with psycopg.connect(_sync_database_url(raw_url), connect_timeout=3):
            pass
    except psycopg.Error:
        pytest.fail("isolated PostgreSQL is unavailable", pytrace=False)
    if not os.environ.get("ADMIN_PASSWORD_HASH_B64"):
        pytest.fail("real Phase 2 integration requires a test-only admin hash", pytrace=False)
    media_root = Path(os.environ.get("MEDIA_STORE_ROOT", ""))
    if media_root != Path("/data/media-store") or _has_symlink_component(media_root):
        pytest.fail("real Phase 2 integration requires the isolated media volume", pytrace=False)
    for child_name in ("public", "archive", "trash", ".staging"):
        child = media_root / child_name
        if child.exists() and _has_symlink_component(child):
            pytest.fail("real Phase 2 integration media path contains a symlink", pytrace=False)
    return raw_url


def _scalar(
    connection: psycopg.Connection, statement: str, parameters: tuple[Any, ...] = ()
) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        row = cursor.fetchone()
    return row[0] if row else None


def _row(
    connection: psycopg.Connection, statement: str, parameters: tuple[Any, ...] = ()
) -> tuple[Any, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return cursor.fetchone()


def _event_rows(connection: psycopg.Connection, request_id: str) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT event_type, response_type, error_code FROM usage_events WHERE request_id = %s",
            (uuid.UUID(request_id),),
        )
        return list(cursor.fetchall())


class _ReconcileLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _assert_public_event(
    connection: psycopg.Connection,
    response,
    *,
    response_type: str | None,
    error_code: str | None,
) -> None:
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert _event_rows(connection, request_id) == [("public_request", response_type, error_code)]


def _json(response) -> dict[str, Any] | list[Any]:
    assert response.headers["Content-Type"].startswith("application/json")
    return response.json()


def _write_real_fixture(path: Path) -> None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000",
            "-t",
            "1.5",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, "ffmpeg could not create the isolated input fixture"
    assert path.is_file() and path.stat().st_size > 0


@pytest.mark.integration
def test_real_admin_media_publish_and_public_search(
    integration_environment: str, tmp_path: Path
) -> None:
    raw_url = integration_environment
    sync_url = _sync_database_url(raw_url)
    prefix = f"phase2-{uuid.uuid4().hex[:10]}"
    target_id: str | None = None
    archived_target_id: str | None = None
    origin_ids: list[str] = []
    lineup_ids: list[str] = []
    conversation_ids: list[str] = []
    request_ids: list[str] = []

    source_path = tmp_path / "phase2-real-input.mp4"
    _write_real_fixture(source_path)
    source_bytes = source_path.read_bytes()

    def remember(response):
        request_ids.append(response.headers["X-Request-ID"])
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("conversation_id"):
            conversation_ids.append(payload["conversation_id"])
        return payload

    def admin_json(client: TestClient, method: str, path: str, payload: object):
        response = client.request(
            method, path, json=payload, headers={**_REQUEST_HEADERS, "Accept": "application/json"}
        )
        return response, remember(response)

    def admin_count(connection: psycopg.Connection, event_type: str = "admin_action") -> int:
        return int(
            _scalar(
                connection, "SELECT count(*) FROM usage_events WHERE event_type = %s", (event_type,)
            )
        )

    def run_reconcile(client: TestClient) -> list[logging.LogRecord]:
        logger = logging.getLogger("smokeline")
        capture = _ReconcileLogCapture()
        previous_level = logger.level
        logger.setLevel(logging.WARNING)
        logger.addHandler(capture)
        try:
            client.portal.call(reconcile_media._run_reconcile_with_lock)
            return list(capture.records)
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)

    connection = psycopg.connect(sync_url, autocommit=True)
    try:
        before_target_count = int(_scalar(connection, "SELECT count(*) FROM targets"))
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/admin/session", headers={"Accept": "application/json"})
            remember(response)
            assert response.status_code == 401
            assert _json(response)["error"]["code"] == "ADMIN_AUTH_REQUIRED"

            response, payload = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    "slug": f"{prefix}-bad",
                    "name_zh": "bad",
                    "map_x": True,
                    "map_y": 50,
                },
            )
            assert response.status_code == 422
            assert payload["error"]["code"] == "VALIDATION_ERROR"
            assert payload["error"]["details"]["fields"][0]["path"] == "map_x"
            assert _scalar(connection, "SELECT count(*) FROM targets") == before_target_count

            target_payload = {
                "slug": f"{prefix}-vip",
                "name_zh": "VIP",
                "name_en": "Window",
                "description": "自动化测试夹具目标",
                "sort_order": 1,
                "aliases": ["VIP", "混合点"],
            }
            response, target = admin_json(client, "POST", "/api/v1/admin/targets", target_payload)
            assert response.status_code == 201
            assert set(target) == {
                "id",
                "slug",
                "name_zh",
                "name_en",
                "description",
                "map_x",
                "map_y",
                "sort_order",
                "aliases",
                "is_active",
                "created_at",
                "updated_at",
            }
            target_id = target["id"]
            position_response, target = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}/position",
                {"map_x": 50, "map_y": 25},
            )
            assert position_response.status_code == 200
            action_before = admin_count(connection)
            rounded_noop_response, rounded_noop = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}/position",
                {"map_x": 50.004, "map_y": 25.004},
            )
            assert rounded_noop_response.status_code == 200
            assert (rounded_noop["map_x"], rounded_noop["map_y"]) == (50.0, 25.0)
            assert admin_count(connection) == action_before
            rounded_change_response, rounded_change = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}/position",
                {"map_x": 50.005, "map_y": 25.005},
            )
            assert rounded_change_response.status_code == 200
            assert (rounded_change["map_x"], rounded_change["map_y"]) == (50.01, 25.01)
            assert admin_count(connection) == action_before + 1
            target = rounded_change

            response, duplicate_target = admin_json(
                client, "POST", "/api/v1/admin/targets", target_payload
            )
            assert response.status_code == 409
            assert duplicate_target["error"]["code"] == "TARGET_SLUG_CONFLICT"

            archived_target_response, archived_target = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    **target_payload,
                    "slug": f"{prefix}-archived",
                    "name_zh": "归档定位边界",
                    "aliases": ["归档定位边界"],
                },
            )
            assert archived_target_response.status_code == 201
            archived_target_id = archived_target["id"]
            archive_response, archived_target = admin_json(
                client,
                "POST",
                f"/api/v1/admin/targets/{archived_target_id}/archive",
                {},
            )
            assert archive_response.status_code == 200
            inactive_position, inactive_body = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{archived_target_id}/position",
                {"map_x": 0, "map_y": 100},
            )
            assert inactive_position.status_code == 409
            assert inactive_body["error"]["code"] == "POINT_INACTIVE"

            origin_payload = {
                "slug": f"{prefix}-t-spawn",
                "name_zh": "T 出生点",
                "name_en": "T Spawn",
                "description": "自动化测试夹具起点",
                "sort_order": 1,
                "aliases": ["T 出生点"],
            }
            response, origin = admin_json(client, "POST", "/api/v1/admin/origins", origin_payload)
            assert response.status_code == 201
            origin_ids.append(origin["id"])

            no_route_origin_payload = {
                **origin_payload,
                "slug": f"{prefix}-ct-spawn",
                "name_zh": "CT 出生点",
                "name_en": "CT Spawn",
                "aliases": ["CT 出生点", "混合点"],
            }
            response, no_route_origin = admin_json(
                client, "POST", "/api/v1/admin/origins", no_route_origin_payload
            )
            assert response.status_code == 201
            origin_ids.append(no_route_origin["id"])

            def create_lineup(sort_order: int, throw_text: str):
                payload = {
                    "target_id": target_id,
                    "origin_id": origin_ids[0],
                    "steps": {
                        "setup": "自动化测试站位",
                        "aim": "自动化测试瞄点",
                        "throw": throw_text,
                        "result": "自动化测试封烟",
                    },
                    "sort_order": sort_order,
                    "check_info": {"last_checked_at": None, "game_version_note": None},
                }
                result, body = admin_json(client, "POST", "/api/v1/admin/lineups", payload)
                assert result.status_code == 201
                assert body["target"] == {
                    "id": target["id"],
                    "slug": target["slug"],
                    "name_zh": target["name_zh"],
                }
                assert "checksum_sha256" not in json.dumps(body, ensure_ascii=False)
                lineup_ids.append(body["id"])
                return body

            create_lineup(1, "自动化测试左键跳投")

            invalid_upload = client.post(
                f"/api/v1/admin/lineups/{lineup_ids[0]}/video",
                files={"file": ("fixture.mp4", source_bytes, "video/mp4")},
                data={"rights_note": ""},
                headers=_REQUEST_HEADERS,
            )
            invalid_upload_body = remember(invalid_upload)
            assert invalid_upload.status_code == 422
            assert invalid_upload_body["error"]["code"] == "VALIDATION_ERROR"
            assert invalid_upload_body["error"]["details"]["fields"][0]["path"] == "rights_note"
            assert (
                _scalar(
                    connection,
                    "SELECT count(*) FROM tutorial_videos WHERE lineup_id = %s",
                    (uuid.UUID(lineup_ids[0]),),
                )
                == 0
            )

            valid_upload = client.post(
                f"/api/v1/admin/lineups/{lineup_ids[0]}/video",
                files={"file": ("fixture.mp4", source_bytes, "video/mp4")},
                data={"rights_note": "自动化测试夹具，非真实教学"},
                headers=_REQUEST_HEADERS,
            )
            uploaded = remember(valid_upload)
            assert valid_upload.status_code == 200
            video = uploaded["primary_video"]
            assert 1 <= video["duration_ms"] <= 2_000
            assert 1 <= video["size_bytes"] <= 12 * 1024 * 1024
            assert video["url"].startswith("/media/") and len(video["url"].split("?v=")[-1]) == 12
            relative_path, status_value = (
                _scalar(
                    connection,
                    "SELECT relative_path FROM tutorial_videos WHERE lineup_id = %s",
                    (uuid.UUID(lineup_ids[0]),),
                ),
                _scalar(
                    connection,
                    "SELECT status FROM tutorial_videos WHERE lineup_id = %s",
                    (uuid.UUID(lineup_ids[0]),),
                ),
            )
            assert status_value == "ready"
            poster_relative_path = _scalar(
                connection,
                "SELECT poster_relative_path FROM tutorial_videos WHERE lineup_id = %s",
                (uuid.UUID(lineup_ids[0]),),
            )
            media_root = Path(os.environ["MEDIA_STORE_ROOT"])
            output_path = media_root / "public" / relative_path
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
            )
            output_payload = json.loads(probe.stdout)
            video_stream = next(
                item for item in output_payload["streams"] if item["codec_type"] == "video"
            )
            audio_stream = next(
                item for item in output_payload["streams"] if item["codec_type"] == "audio"
            )
            assert (video_stream["codec_name"], video_stream["width"], video_stream["height"]) == (
                "h264",
                1920,
                1080,
            )
            assert video_stream["pix_fmt"] == "yuv420p"
            assert audio_stream["codec_name"] == "aac"
            assert (audio_stream["sample_rate"], audio_stream["channels"]) == ("48000", 2)

            response, not_default = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{lineup_ids[0]}/publish",
                {"make_default": False},
            )
            assert response.status_code == 409
            assert not_default["error"]["code"] == "DEFAULT_REQUIRED"
            response, published = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{lineup_ids[0]}/publish",
                {"make_default": True},
            )
            assert response.status_code == 200
            assert published["status"] == "published" and published["is_default"] is True
            clear_published, clear_published_body = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}/position",
                {"map_x": None, "map_y": None},
            )
            assert clear_published.status_code == 409
            assert clear_published_body["error"]["code"] == "PUBLISHED_TARGET_POSITION_REQUIRED"
            action_count_before_idempotent_publish = admin_count(connection)
            response, idempotent = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{lineup_ids[0]}/publish",
                {"make_default": True},
            )
            assert response.status_code == 200 and idempotent["id"] == lineup_ids[0]
            assert admin_count(connection) == action_count_before_idempotent_publish

            response, blocked_patch = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/lineups/{lineup_ids[0]}",
                {"steps": {"setup": "不能改", "aim": "", "throw": "", "result": ""}},
            )
            assert response.status_code == 409
            assert blocked_patch["error"]["code"] == "PUBLISHED_LINEUP_IMMUTABLE"

            create_lineup(2, "自动化测试蹲投")
            valid_second = client.post(
                f"/api/v1/admin/lineups/{lineup_ids[1]}/video",
                files={"file": ("fixture.mp4", source_bytes, "video/mp4")},
                data={"rights_note": "自动化测试夹具，非真实教学"},
                headers=_REQUEST_HEADERS,
            )
            second_uploaded = remember(valid_second)
            assert valid_second.status_code == 200
            second_relative_path = (
                second_uploaded["primary_video"]["url"].split("?v=")[0].removeprefix("/media/")
            )
            corrupt = client.post(
                f"/api/v1/admin/lineups/{lineup_ids[1]}/video",
                files={"file": ("corrupt.mp4", b"not an mp4", "video/mp4")},
                data={"rights_note": "自动化测试夹具，非真实教学"},
                headers=_REQUEST_HEADERS,
            )
            corrupt_body = remember(corrupt)
            assert corrupt.status_code == 422 and corrupt_body["error"]["code"] == "INVALID_VIDEO"
            retained_status, retained_path = (
                _scalar(
                    connection,
                    "SELECT status FROM tutorial_videos WHERE lineup_id = %s",
                    (uuid.UUID(lineup_ids[1]),),
                ),
                _scalar(
                    connection,
                    "SELECT relative_path FROM tutorial_videos WHERE lineup_id = %s",
                    (uuid.UUID(lineup_ids[1]),),
                ),
            )
            assert retained_status == "ready" and retained_path == second_relative_path

            response, second_published = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{lineup_ids[1]}/publish",
                {"make_default": False},
            )
            assert response.status_code == 200
            assert second_published["is_default"] is False

            published_upload = client.post(
                f"/api/v1/admin/lineups/{lineup_ids[0]}/video",
                files={"file": ("corrupt.mp4", b"not an mp4", "video/mp4")},
                data={"rights_note": "自动化测试夹具，非真实教学"},
                headers=_REQUEST_HEADERS,
            )
            published_upload_body = remember(published_upload)
            assert published_upload.status_code == 409
            assert published_upload_body["error"]["code"] == "PUBLISHED_LINEUP_IMMUTABLE"

            lineups_response = client.get(
                "/api/v1/admin/lineups?status=all",
                headers={**_REQUEST_HEADERS, "Accept": "application/json"},
            )
            lineups_body = remember(lineups_response)
            assert lineups_response.status_code == 200
            assert {lineup["id"] for lineup in lineups_body} >= set(lineup_ids)
            assert all(
                "checksum_sha256" not in json.dumps(lineup, ensure_ascii=False)
                for lineup in lineups_body
            )

            standard = client.post(
                "/api/v1/search",
                json={
                    "kind": "query",
                    "text": "我在 Mirage 的 T 出生点，想封 VIP 烟",
                    "input_mode": "text",
                },
            )
            standard_body = remember(standard)
            assert standard.status_code == 200 and standard_body["type"] == "match"
            assert standard_body["lineup"]["id"] == lineup_ids[0]

            target_only = client.post(
                "/api/v1/search",
                json={"kind": "query", "text": "VIP 烟", "input_mode": "text"},
            )
            target_only_body = remember(target_only)
            assert target_only.status_code == 200 and target_only_body["type"] == "route_matches"
            assert target_only_body["intent"]["origin_slug"] is None
            assert len(target_only_body["routes"]) == 1

            explicit_missing = client.post(
                "/api/v1/search",
                json={"kind": "query", "text": "CT 出生点 VIP 烟", "input_mode": "text"},
            )
            explicit_missing_body = remember(explicit_missing)
            assert explicit_missing.status_code == 200
            assert explicit_missing_body["type"] == "not_found"
            assert explicit_missing_body["reason"] == "NO_LOCAL_RESULT"

            cross_field = client.post(
                "/api/v1/search",
                json={"kind": "query", "text": "混合点 VIP 烟", "input_mode": "text"},
            )
            cross_field_body = remember(cross_field)
            assert cross_field.status_code == 503
            assert cross_field_body["error"]["code"] == "INTENT_AI_UNAVAILABLE"

            detail = client.get(f"/api/v1/lineups/{lineup_ids[0]}")
            detail_body = remember(detail)
            assert detail.status_code == 200 and detail_body["id"] == lineup_ids[0]
            assert "checksum_sha256" not in json.dumps(detail_body, ensure_ascii=False)

            # Reconciliation is called by its real startup/script entry point,
            # which acquires the media lock before entering the lock-free
            # reconcile_media() implementation.  Two healthy passes must not
            # alter either published route or emit a replacement warning.
            initial_lineup_state = {
                lineup_id: _row(
                    connection,
                    "SELECT status, is_default, default_needs_review FROM lineups WHERE id = %s",
                    (uuid.UUID(lineup_id),),
                )
                for lineup_id in lineup_ids[:2]
            }
            healthy_records = run_reconcile(client)
            healthy_records += run_reconcile(client)
            assert not [
                record
                for record in healthy_records
                if getattr(record, "event", None) == "MEDIA_DEFAULT_REPLACED"
            ]
            assert {
                lineup_id: _row(
                    connection,
                    "SELECT status, is_default, default_needs_review FROM lineups WHERE id = %s",
                    (uuid.UUID(lineup_id),),
                )
                for lineup_id in lineup_ids[:2]
            } == initial_lineup_state

            default_video_path = media_root / "public" / relative_path
            default_poster_path = media_root / "public" / poster_relative_path
            assert default_video_path.is_file() and default_poster_path.is_file()
            default_video_path.write_bytes(b"not an mp4")

            # Corrupt only this test's default video.  Reconcile must isolate
            # both pair members, clear the DB pointers, and choose the healthy
            # sibling as a review-needed default.
            replacement_records = run_reconcile(client)
            replacement_records = [
                record
                for record in replacement_records
                if getattr(record, "event", None) == "MEDIA_DEFAULT_REPLACED"
            ]
            assert len(replacement_records) == 1
            assert _row(
                connection,
                "SELECT status, is_default, default_needs_review FROM lineups WHERE id = %s",
                (uuid.UUID(lineup_ids[0]),),
            ) == ("needs_revalidation", False, False)
            assert _row(
                connection,
                "SELECT status, is_default, default_needs_review FROM lineups WHERE id = %s",
                (uuid.UUID(lineup_ids[1]),),
            ) == ("published", True, True)
            assert _row(
                connection,
                "SELECT status, storage_key, relative_path, poster_relative_path, "
                "duration_ms, size_bytes, checksum_sha256, failure_reason "
                "FROM tutorial_videos WHERE lineup_id = %s",
                (uuid.UUID(lineup_ids[0]),),
            ) == ("failed", None, None, None, None, None, None, "media_corrupt")
            assert not default_video_path.exists() and not default_poster_path.exists()
            assert len(list((media_root / "trash").rglob(default_video_path.name))) == 1
            assert len(list((media_root / "trash").rglob(default_poster_path.name))) == 1

            recovered = client.post(
                "/api/v1/search",
                json={
                    "kind": "query",
                    "text": "我在 Mirage 的 T 出生点，想封 VIP 烟",
                    "input_mode": "text",
                },
            )
            recovered_body = remember(recovered)
            assert recovered.status_code == 200
            assert recovered_body["type"] == "match"
            assert recovered_body["lineup"]["id"] == lineup_ids[1]
            unavailable_detail = client.get(f"/api/v1/lineups/{lineup_ids[0]}")
            unavailable_detail_body = remember(unavailable_detail)
            assert unavailable_detail.status_code == 404
            assert unavailable_detail_body["error"]["code"] == "LINEUP_NOT_FOUND"

            # A second damaged-state pass is a no-op: no duplicate movement,
            # warning, or default flip is allowed.
            recovered_lineup_state = {
                lineup_id: _row(
                    connection,
                    "SELECT status, is_default, default_needs_review FROM lineups WHERE id = %s",
                    (uuid.UUID(lineup_id),),
                )
                for lineup_id in lineup_ids[:2]
            }
            final_records = run_reconcile(client)
            assert not [
                record
                for record in final_records
                if getattr(record, "event", None) == "MEDIA_DEFAULT_REPLACED"
            ]
            assert {
                lineup_id: _row(
                    connection,
                    "SELECT status, is_default, default_needs_review FROM lineups WHERE id = %s",
                    (uuid.UUID(lineup_id),),
                )
                for lineup_id in lineup_ids[:2]
            } == recovered_lineup_state
            assert len(list((media_root / "trash").rglob(default_video_path.name))) == 1
            assert len(list((media_root / "trash").rglob(default_poster_path.name))) == 1

        _assert_public_event(connection, standard, response_type="match", error_code=None)
        _assert_public_event(
            connection, target_only, response_type="route_matches", error_code=None
        )
        _assert_public_event(
            connection,
            explicit_missing,
            response_type="not_found",
            error_code=None,
        )
        _assert_public_event(
            connection, cross_field, response_type=None, error_code="INTENT_AI_UNAVAILABLE"
        )
        _assert_public_event(connection, detail, response_type=None, error_code=None)
        _assert_public_event(connection, recovered, response_type="match", error_code=None)
        _assert_public_event(
            connection,
            unavailable_detail,
            response_type=None,
            error_code="LINEUP_NOT_FOUND",
        )
    finally:
        for request_id in request_ids:
            connection.execute(
                "DELETE FROM usage_events WHERE request_id = %s", (uuid.UUID(request_id),)
            )
        for conversation_id in conversation_ids:
            connection.execute(
                "DELETE FROM conversation_states WHERE id = %s", (uuid.UUID(conversation_id),)
            )
        for lineup_id in lineup_ids:
            connection.execute(
                "DELETE FROM tutorial_videos WHERE lineup_id = %s", (uuid.UUID(lineup_id),)
            )
        for lineup_id in lineup_ids:
            connection.execute("DELETE FROM lineups WHERE id = %s", (uuid.UUID(lineup_id),))
        if target_id:
            connection.execute(
                "DELETE FROM target_aliases WHERE target_id = %s", (uuid.UUID(target_id),)
            )
            connection.execute("DELETE FROM targets WHERE id = %s", (uuid.UUID(target_id),))
        if archived_target_id:
            connection.execute(
                "DELETE FROM target_aliases WHERE target_id = %s",
                (uuid.UUID(archived_target_id),),
            )
            connection.execute(
                "DELETE FROM targets WHERE id = %s", (uuid.UUID(archived_target_id),)
            )
        for origin_id in origin_ids:
            connection.execute(
                "DELETE FROM origin_aliases WHERE origin_id = %s", (uuid.UUID(origin_id),)
            )
            connection.execute("DELETE FROM origins WHERE id = %s", (uuid.UUID(origin_id),))
        connection.close()
        media_root = Path(os.environ.get("MEDIA_STORE_ROOT", ""))
        if media_root == Path("/data/media-store") and not _has_symlink_component(media_root):
            for lineup_id in lineup_ids:
                for root_name in ("public", "archive"):
                    candidate = media_root / root_name / lineup_id
                    root_dir = media_root / root_name
                    if (
                        root_dir.exists()
                        and not _has_symlink_component(root_dir)
                        and candidate.exists()
                        and not candidate.is_symlink()
                        and candidate.resolve(strict=False).parent == root_dir.resolve()
                    ):
                        _remove_test_media_tree(candidate, media_root)
                trash_root = media_root / "trash"
                if trash_root.exists() and not _has_symlink_component(trash_root):
                    for day_dir in trash_root.iterdir():
                        if day_dir.is_symlink() or not day_dir.is_dir():
                            continue
                        for root_name in ("public", "archive"):
                            _remove_test_media_tree(
                                day_dir / root_name / lineup_id,
                                media_root,
                            )


@pytest.mark.integration
def test_real_historical_throw_fk_is_ignored_by_runtime(
    integration_environment: str, tmp_path: Path
) -> None:
    """Keep an old inactive FK while the v1.2 runtime uses descriptive text only."""

    raw_url = integration_environment
    sync_url = _sync_database_url(raw_url)
    prefix = f"phase2-legacy-{uuid.uuid4().hex[:10]}"
    target_id: str | None = None
    origin_id: str | None = None
    lineup_ids: list[str] = []
    request_ids: list[str] = []
    conversation_ids: list[str] = []
    method_id = uuid.uuid4()
    media_root = Path(os.environ["MEDIA_STORE_ROOT"])

    def remember(response):
        request_ids.append(response.headers["X-Request-ID"])
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("conversation_id"):
            conversation_ids.append(payload["conversation_id"])
        return payload

    def admin_json(client: TestClient, method: str, path: str, payload: object):
        response = client.request(
            method, path, json=payload, headers={**_REQUEST_HEADERS, "Accept": "application/json"}
        )
        return response, remember(response)

    def run_reconcile(client: TestClient) -> list[logging.LogRecord]:
        logger = logging.getLogger("smokeline")
        capture = _ReconcileLogCapture()
        previous_level = logger.level
        logger.setLevel(logging.WARNING)
        logger.addHandler(capture)
        try:
            client.portal.call(reconcile_media._run_reconcile_with_lock)
            return list(capture.records)
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)

    def cleanup_media() -> None:
        if media_root != Path("/data/media-store") or _has_symlink_component(media_root):
            return
        for lineup_id in lineup_ids:
            for root_name in ("public", "archive"):
                candidate = media_root / root_name / lineup_id
                if candidate.exists():
                    _remove_test_media_tree(candidate, media_root)
            trash_root = media_root / "trash"
            if not trash_root.exists() or _has_symlink_component(trash_root):
                continue
            for day_dir in trash_root.iterdir():
                if day_dir.is_symlink() or not day_dir.is_dir():
                    continue
                for root_name in ("public", "archive"):
                    candidate = day_dir / root_name / lineup_id
                    if candidate.exists():
                        _remove_test_media_tree(candidate, media_root)

    connection = psycopg.connect(sync_url, autocommit=True)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            target_response, target = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    "slug": f"{prefix}-target",
                    "name_zh": "历史FK目标",
                    "name_en": "Legacy FK Target",
                    "description": "隔离历史FK回归夹具",
                    "sort_order": 1,
                    "aliases": ["历史FK目标"],
                },
            )
            assert target_response.status_code == 201
            target_id = target["id"]
            position_response, target = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}/position",
                {"map_x": 40, "map_y": 40},
            )
            assert position_response.status_code == 200
            origin_response, origin = admin_json(
                client,
                "POST",
                "/api/v1/admin/origins",
                {
                    "slug": f"{prefix}-origin",
                    "name_zh": "历史FK起点",
                    "name_en": "Legacy FK Origin",
                    "description": "隔离历史FK回归夹具",
                    "sort_order": 1,
                    "aliases": ["历史FK起点"],
                },
            )
            assert origin_response.status_code == 201
            origin_id = origin["id"]

            method_created_at = datetime.now(UTC)
            connection.execute(
                """
                INSERT INTO throw_methods
                    (id, slug, name_zh, search_term_en, sort_order, is_active,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, false, %s, %s)
                """,
                (
                    method_id,
                    f"{prefix}-inactive-method",
                    "历史停用投法",
                    "legacy inactive method",
                    999,
                    method_created_at,
                    method_created_at,
                ),
            )

            def create_lineup() -> str:
                response, payload = admin_json(
                    client,
                    "POST",
                    "/api/v1/admin/lineups",
                    {
                        "target_id": target_id,
                        "origin_id": origin_id,
                        "steps": {
                            "setup": "历史兼容站位",
                            "aim": "历史兼容瞄点",
                            "throw": "历史投掷说明只是普通文字",
                            "result": "历史兼容结果",
                        },
                        "sort_order": len(lineup_ids) + 1,
                        "check_info": {"last_checked_at": None, "game_version_note": None},
                    },
                )
                assert response.status_code == 201
                assert "throw_method_id" not in payload
                lineup_id = payload["id"]
                lineup_ids.append(lineup_id)
                return lineup_id

            legacy_lineup_id = create_lineup()
            null_lineup_id = create_lineup()
            connection.execute(
                "UPDATE lineups SET throw_method_id = %s WHERE id = %s",
                (method_id, uuid.UUID(legacy_lineup_id)),
            )
            assert _row(
                connection,
                "SELECT throw_method_id FROM lineups WHERE id = %s",
                (uuid.UUID(legacy_lineup_id),),
            ) == (method_id,)
            assert _row(
                connection,
                "SELECT throw_method_id FROM lineups WHERE id = %s",
                (uuid.UUID(null_lineup_id),),
            ) == (None,)

            historical_method_before = _row(
                connection,
                "SELECT id, slug, name_zh, search_term_en, sort_order, is_active, "
                "created_at, updated_at FROM throw_methods WHERE id = %s",
                (method_id,),
            )
            historical_lineup_before = _row(
                connection,
                "SELECT target_id, origin_id, throw_method_id, setup_text, aim_text, "
                "throw_text, result_text FROM lineups WHERE id = %s",
                (uuid.UUID(legacy_lineup_id),),
            )

            source_path = tmp_path / "phase2-legacy-fk-input.mp4"
            _write_real_fixture(source_path)
            source_bytes = source_path.read_bytes()
            for lineup_id in lineup_ids:
                response = client.post(
                    f"/api/v1/admin/lineups/{lineup_id}/video",
                    files={"file": (f"{lineup_id}.mp4", source_bytes, "video/mp4")},
                    data={"rights_note": "隔离测试自生成视频，非真实教学"},
                    headers=_REQUEST_HEADERS,
                )
                payload = remember(response)
                assert response.status_code == 200
                assert payload["primary_video"]["status"] == "ready"

            response, published_legacy = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{legacy_lineup_id}/publish",
                {"make_default": True},
            )
            assert response.status_code == 200
            assert published_legacy["status"] == "published"
            assert published_legacy["is_default"] is True

            legacy_detail = client.get(f"/api/v1/lineups/{legacy_lineup_id}")
            legacy_detail_body = remember(legacy_detail)
            assert legacy_detail.status_code == 200
            assert legacy_detail_body["id"] == legacy_lineup_id
            assert "throw_method_id" not in json.dumps(legacy_detail_body, ensure_ascii=False)

            legacy_search = client.post(
                "/api/v1/search",
                json={
                    "kind": "query",
                    "text": "我在 历史FK起点，想封 历史FK目标",
                    "input_mode": "text",
                },
            )
            legacy_search_body = remember(legacy_search)
            assert legacy_search.status_code == 200
            assert legacy_search_body["type"] == "match"
            assert legacy_search_body["lineup"]["id"] == legacy_lineup_id

            response, published_null = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{null_lineup_id}/publish",
                {"make_default": True},
            )
            assert response.status_code == 200
            assert published_null["status"] == "published"
            assert published_null["is_default"] is True

            null_detail = client.get(f"/api/v1/lineups/{null_lineup_id}")
            null_detail_body = remember(null_detail)
            assert null_detail.status_code == 200
            assert null_detail_body["id"] == null_lineup_id
            assert "throw_method_id" not in json.dumps(null_detail_body, ensure_ascii=False)

            null_search = client.post(
                "/api/v1/search",
                json={
                    "kind": "query",
                    "text": "我在 历史FK起点，想封 历史FK目标",
                    "input_mode": "text",
                },
            )
            null_search_body = remember(null_search)
            assert null_search.status_code == 200
            assert null_search_body["type"] == "match"
            assert null_search_body["lineup"]["id"] == null_lineup_id

            legacy_state_before_reconcile = _row(
                connection,
                "SELECT status, is_default, default_needs_review, throw_method_id "
                "FROM lineups WHERE id = %s",
                (uuid.UUID(legacy_lineup_id),),
            )
            null_state_before_reconcile = _row(
                connection,
                "SELECT status, is_default, default_needs_review, throw_method_id "
                "FROM lineups WHERE id = %s",
                (uuid.UUID(null_lineup_id),),
            )
            records = run_reconcile(client)
            assert not [
                record
                for record in records
                if getattr(record, "event", None) == "MEDIA_DEFAULT_REPLACED"
            ]
            assert (
                _row(
                    connection,
                    "SELECT status, is_default, default_needs_review, throw_method_id "
                    "FROM lineups WHERE id = %s",
                    (uuid.UUID(legacy_lineup_id),),
                )
                == legacy_state_before_reconcile
            )
            assert (
                _row(
                    connection,
                    "SELECT status, is_default, default_needs_review, throw_method_id "
                    "FROM lineups WHERE id = %s",
                    (uuid.UUID(null_lineup_id),),
                )
                == null_state_before_reconcile
            )
            assert (
                _row(
                    connection,
                    "SELECT id, slug, name_zh, search_term_en, sort_order, is_active, "
                    "created_at, updated_at FROM throw_methods WHERE id = %s",
                    (method_id,),
                )
                == historical_method_before
            )
            assert (
                _row(
                    connection,
                    "SELECT target_id, origin_id, throw_method_id, setup_text, aim_text, "
                    "throw_text, result_text FROM lineups WHERE id = %s",
                    (uuid.UUID(legacy_lineup_id),),
                )
                == historical_lineup_before
            )

            legacy_after_reconcile = client.get(f"/api/v1/lineups/{legacy_lineup_id}")
            legacy_after_reconcile_body = remember(legacy_after_reconcile)
            assert legacy_after_reconcile.status_code == 200
            assert legacy_after_reconcile_body["id"] == legacy_lineup_id
            final_search = client.post(
                "/api/v1/search",
                json={
                    "kind": "query",
                    "text": "我在 历史FK起点，想封 历史FK目标",
                    "input_mode": "text",
                },
            )
            final_search_body = remember(final_search)
            assert final_search.status_code == 200
            assert final_search_body["type"] == "match"
            assert final_search_body["lineup"]["id"] == null_lineup_id
    finally:
        for request_id in request_ids:
            connection.execute(
                "DELETE FROM usage_events WHERE request_id = %s", (uuid.UUID(request_id),)
            )
        for conversation_id in conversation_ids:
            connection.execute(
                "DELETE FROM conversation_states WHERE id = %s", (uuid.UUID(conversation_id),)
            )
        for lineup_id in lineup_ids:
            connection.execute(
                "DELETE FROM tutorial_videos WHERE lineup_id = %s", (uuid.UUID(lineup_id),)
            )
        for lineup_id in lineup_ids:
            connection.execute("DELETE FROM lineups WHERE id = %s", (uuid.UUID(lineup_id),))
        connection.execute("DELETE FROM throw_methods WHERE id = %s", (method_id,))
        if target_id:
            connection.execute(
                "DELETE FROM target_aliases WHERE target_id = %s", (uuid.UUID(target_id),)
            )
            connection.execute("DELETE FROM targets WHERE id = %s", (uuid.UUID(target_id),))
        if origin_id:
            connection.execute(
                "DELETE FROM origin_aliases WHERE origin_id = %s", (uuid.UUID(origin_id),)
            )
            connection.execute("DELETE FROM origins WHERE id = %s", (uuid.UUID(origin_id),))
        connection.close()
        cleanup_media()


@pytest.mark.integration
def test_real_unlocated_points_publish_and_remain_visible(
    integration_environment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise NULL and located points through one real PG/FFmpeg/API flow."""

    placeholder_catalog = SimpleNamespace(
        slug="mirage",
        name_zh="荒漠迷城",
        asset_url="/assets/maps/mirage.svg",
        asset_status="placeholder",
    )
    monkeypatch.setattr("app.api.admin.get_map_catalog", lambda: placeholder_catalog)
    monkeypatch.setattr("app.api.public.get_map_catalog", lambda: placeholder_catalog)

    sync_url = _sync_database_url(integration_environment)
    prefix = f"phase2-null-coord-{uuid.uuid4().hex[:10]}"
    target_ids: list[str] = []
    origin_ids: list[str] = []
    lineup_ids: list[str] = []
    request_ids: list[str] = []
    conversation_ids: list[str] = []
    public_events: list[tuple[object, str | None, str | None]] = []
    media_root = Path(os.environ["MEDIA_STORE_ROOT"])

    def remember(response):
        request_ids.append(response.headers["X-Request-ID"])
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("conversation_id"):
            conversation_ids.append(payload["conversation_id"])
        return payload

    def admin_json(client: TestClient, method: str, path: str, payload: object):
        response = client.request(
            method,
            path,
            json=payload,
            headers={**_REQUEST_HEADERS, "Accept": "application/json"},
        )
        return response, remember(response)

    def public_json(client: TestClient, method: str, path: str, **kwargs):
        response = client.request(method, path, **kwargs)
        payload = remember(response)
        return response, payload

    def record_public(response, response_type: str | None, error_code: str | None, payload):
        public_events.append((response, response_type, error_code))
        return payload

    source_path = tmp_path / "phase2-null-coordinate-input.mp4"
    _write_real_fixture(source_path)
    source_bytes = source_path.read_bytes()
    connection = psycopg.connect(sync_url, autocommit=True)
    try:
        provider_before = int(
            _scalar(
                connection,
                "SELECT count(*) FROM usage_events WHERE event_type = 'provider_reservation'",
            )
        )
        before_target_count = int(_scalar(connection, "SELECT count(*) FROM targets"))
        with TestClient(app, raise_server_exceptions=False) as client:
            invalid_response, invalid_body = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    "slug": f"{prefix}-half-null",
                    "name_zh": "半空坐标",
                    "map_x": None,
                    "map_y": 1,
                },
            )
            assert invalid_response.status_code == 422
            assert invalid_body["error"]["code"] == "VALIDATION_ERROR"
            assert {field["path"] for field in invalid_body["error"]["details"]["fields"]} == {
                "map_x",
                "map_y",
            }
            assert int(_scalar(connection, "SELECT count(*) FROM targets")) == before_target_count

            target_response, target = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    "slug": f"{prefix}-vip-null",
                    "name_zh": "未定位 VIP",
                    "name_en": "Unlocated VIP",
                    "description": "坐标尚未核验的隔离测试目标",
                    "sort_order": 20,
                    "aliases": ["未定位烟位"],
                },
            )
            assert target_response.status_code == 201
            assert target["map_x"] is None and target["map_y"] is None
            target_ids.append(target["id"])

            located_target_response, located_target = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    "slug": f"{prefix}-vip-located",
                    "name_zh": "未定位 VIP",
                    "name_en": "Located Variant",
                    "description": "含真实边界坐标的隔离测试目标",
                    "sort_order": 1,
                    "aliases": ["混合坐标目标"],
                },
            )
            assert located_target_response.status_code == 201
            located_position_response, located_target = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{located_target['id']}/position",
                {"map_x": 0, "map_y": 100},
            )
            assert located_position_response.status_code == 200
            assert (located_target["map_x"], located_target["map_y"]) == (0, 100)
            target_ids.append(located_target["id"])

            origin_response, origin = admin_json(
                client,
                "POST",
                "/api/v1/admin/origins",
                {
                    "slug": f"{prefix}-spawn-null",
                    "name_zh": "未定位出生点",
                    "description": "坐标尚未核验的隔离测试起点",
                    "sort_order": 20,
                    "aliases": ["未定位出生点"],
                },
            )
            assert origin_response.status_code == 201
            assert origin["map_x"] is None and origin["map_y"] is None
            origin_ids.append(origin["id"])

            located_origin_response, located_origin = admin_json(
                client,
                "POST",
                "/api/v1/admin/origins",
                {
                    "slug": f"{prefix}-spawn-located",
                    "name_zh": "已定位出生点",
                    "description": "含0边界坐标的隔离测试起点",
                    "sort_order": 1,
                    "aliases": ["已定位出生点"],
                },
            )
            assert located_origin_response.status_code == 201
            assert located_origin["map_x"] is None and located_origin["map_y"] is None
            origin_ids.append(located_origin["id"])

            def create_lineup(
                target_ref: str,
                origin_ref: str,
                sort_order: int,
                label: str,
                make_default: bool,
            ) -> str:
                response, body = admin_json(
                    client,
                    "POST",
                    "/api/v1/admin/lineups",
                    {
                        "target_id": target_ref,
                        "origin_id": origin_ref,
                        "steps": {
                            "setup": f"{label}站位",
                            "aim": f"{label}瞄点",
                            "throw": f"{label}普通投掷说明",
                            "result": f"{label}结果",
                        },
                        "sort_order": sort_order,
                        "check_info": {"last_checked_at": None, "game_version_note": None},
                    },
                )
                assert response.status_code == 201
                lineup_ref = body["id"]
                lineup_ids.append(lineup_ref)
                upload = client.post(
                    f"/api/v1/admin/lineups/{lineup_ref}/video",
                    files={"file": (f"{label}.mp4", source_bytes, "video/mp4")},
                    data={"rights_note": "隔离测试自生成视频，非真实教学"},
                    headers=_REQUEST_HEADERS,
                )
                uploaded = remember(upload)
                assert upload.status_code == 200
                assert uploaded["primary_video"]["status"] == "ready"
                published_response, published = admin_json(
                    client,
                    "POST",
                    f"/api/v1/admin/lineups/{lineup_ref}/publish",
                    {"make_default": make_default},
                )
                assert published_response.status_code == 200
                assert published["is_default"] is make_default
                return lineup_ref

            null_default_id = create_lineup(target["id"], origin["id"], 99, "未定位默认", True)
            null_sibling_id = create_lineup(target["id"], origin["id"], 1, "未定位变体", False)
            null_located_origin_id = create_lineup(
                target["id"], located_origin["id"], 2, "未定位已定位起点", True
            )
            located_id = create_lineup(
                located_target["id"], located_origin["id"], 3, "边界坐标", True
            )

            admin_targets = client.get(
                "/api/v1/admin/targets", headers={**_REQUEST_HEADERS, "Accept": "application/json"}
            )
            admin_targets_body = remember(admin_targets)
            assert admin_targets.status_code == 200
            admin_target_by_slug = {row["slug"]: row for row in admin_targets_body}
            assert admin_target_by_slug[target["slug"]]["map_x"] is None
            assert (
                admin_target_by_slug[located_target["slug"]]["map_x"],
                admin_target_by_slug[located_target["slug"]]["map_y"],
            ) == (0, 100)

            admin_origins = client.get(
                "/api/v1/admin/origins", headers={**_REQUEST_HEADERS, "Accept": "application/json"}
            )
            admin_origins_body = remember(admin_origins)
            assert admin_origins.status_code == 200
            admin_origin_by_slug = {row["slug"]: row for row in admin_origins_body}
            assert admin_origin_by_slug[origin["slug"]]["map_y"] is None
            assert admin_origin_by_slug[located_origin["slug"]]["map_x"] is None
            assert admin_origin_by_slug[located_origin["slug"]]["map_y"] is None

            admin_lineups = client.get(
                "/api/v1/admin/lineups?status=all",
                headers={**_REQUEST_HEADERS, "Accept": "application/json"},
            )
            admin_lineups_body = remember(admin_lineups)
            assert admin_lineups.status_code == 200
            assert {row["id"] for row in admin_lineups_body} >= set(lineup_ids)
            assert {
                row["id"]: row["is_default"]
                for row in admin_lineups_body
                if row["id"] in lineup_ids
            } == {
                null_default_id: True,
                null_sibling_id: False,
                null_located_origin_id: True,
                located_id: True,
            }

            map_response, map_body = public_json(client, "GET", "/api/v1/maps/mirage")
            map_body = record_public(map_response, None, None, map_body)
            assert map_response.status_code == 200
            assert map_body["map"]["asset_status"] == "placeholder"
            assert map_body["published_lineup_count"] == 4
            map_targets = {row["slug"]: row for row in map_body["targets"]}
            assert map_targets[target["slug"]]["map_x"] is None
            assert map_targets[target["slug"]]["origin_count"] == 2
            assert (
                map_targets[located_target["slug"]]["map_x"],
                map_targets[located_target["slug"]]["map_y"],
            ) == (0, 100)

            target_lineups, target_body = public_json(
                client, "GET", f"/api/v1/targets/{target['slug']}/lineups"
            )
            target_body = record_public(target_lineups, None, None, target_body)
            assert target_lineups.status_code == 200
            assert target_body["target"]["map_x"] is None
            assert [item["origin"]["slug"] for item in target_body["origins"]] == [
                located_origin["slug"],
                origin["slug"],
            ]
            choices_by_origin = {item["origin"]["slug"]: item for item in target_body["origins"]}
            assert choices_by_origin[origin["slug"]]["lineup"]["id"] == null_default_id
            assert (
                choices_by_origin[located_origin["slug"]]["lineup"]["id"] == null_located_origin_id
            )

            detail, detail_body = public_json(client, "GET", f"/api/v1/lineups/{null_default_id}")
            detail_body = record_public(detail, None, None, detail_body)
            assert detail.status_code == 200
            assert detail_body["id"] == null_default_id
            assert detail_body["target"]["map_x"] is None
            assert detail_body["origin"]["map_y"] is None
            assert [item["id"] for item in detail_body["other_lineups"]] == [null_sibling_id]

            standard, standard_body = public_json(
                client,
                "POST",
                "/api/v1/search",
                json={
                    "kind": "query",
                    "text": "从未定位出生点封未定位烟位",
                    "input_mode": "text",
                },
            )
            standard_body = record_public(standard, "match", None, standard_body)
            assert standard.status_code == 200
            assert standard_body["lineup"]["id"] == null_default_id
            assert standard_body["lineup"]["target"]["map_y"] is None

            target_only, target_only_body = public_json(
                client,
                "POST",
                "/api/v1/search",
                json={"kind": "query", "text": "未定位烟位", "input_mode": "text"},
            )
            target_only_body = record_public(target_only, "route_matches", None, target_only_body)
            assert target_only.status_code == 200
            assert target_only_body["type"] == "route_matches"
            assert target_only_body["routes"][0]["target"]["map_x"] is None
            assert [item["origin"]["slug"] for item in target_only_body["routes"]] == [
                located_origin["slug"],
                origin["slug"],
            ]

            clarification, clarification_body = public_json(
                client,
                "POST",
                "/api/v1/search",
                json={"kind": "query", "text": "未定位 VIP", "input_mode": "text"},
            )
            clarification_body = record_public(
                clarification, "clarification", None, clarification_body
            )
            assert clarification.status_code == 200
            assert clarification_body["type"] == "clarification"
            assert {item["value"] for item in clarification_body["options"]} >= {
                target["slug"],
                located_target["slug"],
            }
            selection, selection_body = public_json(
                client,
                "POST",
                "/api/v1/search",
                json={
                    "kind": "selection",
                    "conversation_id": clarification_body["conversation_id"],
                    "selection": {"field": "target", "value": target["slug"]},
                },
            )
            selection_body = record_public(selection, "route_matches", None, selection_body)
            assert selection.status_code == 200
            assert selection_body["type"] == "route_matches"
            assert selection_body["routes"][0]["target"]["map_x"] is None
            assert selection_body["routes"][1]["lineup"]["id"] == null_default_id

            located_detail, located_detail_body = public_json(
                client, "GET", f"/api/v1/lineups/{located_id}"
            )
            located_detail_body = record_public(located_detail, None, None, located_detail_body)
            assert located_detail.status_code == 200
            assert (
                located_detail_body["target"]["map_x"],
                located_detail_body["target"]["map_y"],
            ) == (0, 100)
            assert located_detail_body["origin"]["map_x"] is None
            assert located_detail_body["origin"]["map_y"] is None

        provider_after = int(
            _scalar(
                connection,
                "SELECT count(*) FROM usage_events WHERE event_type = 'provider_reservation'",
            )
        )
        assert provider_after == provider_before
        for response, response_type, error_code in public_events:
            _assert_public_event(
                connection, response, response_type=response_type, error_code=error_code
            )
    finally:
        for request_id in request_ids:
            connection.execute(
                "DELETE FROM usage_events WHERE request_id = %s", (uuid.UUID(request_id),)
            )
        for conversation_id in conversation_ids:
            connection.execute(
                "DELETE FROM conversation_states WHERE id = %s", (uuid.UUID(conversation_id),)
            )
        for lineup_ref in lineup_ids:
            connection.execute(
                "DELETE FROM tutorial_videos WHERE lineup_id = %s", (uuid.UUID(lineup_ref),)
            )
        for lineup_ref in lineup_ids:
            connection.execute("DELETE FROM lineups WHERE id = %s", (uuid.UUID(lineup_ref),))
        for target_ref in target_ids:
            connection.execute(
                "DELETE FROM target_aliases WHERE target_id = %s", (uuid.UUID(target_ref),)
            )
            connection.execute("DELETE FROM targets WHERE id = %s", (uuid.UUID(target_ref),))
        for origin_ref in origin_ids:
            connection.execute(
                "DELETE FROM origin_aliases WHERE origin_id = %s", (uuid.UUID(origin_ref),)
            )
            connection.execute("DELETE FROM origins WHERE id = %s", (uuid.UUID(origin_ref),))
        connection.close()
        if media_root == Path("/data/media-store") and not _has_symlink_component(media_root):
            for lineup_ref in lineup_ids:
                for root_name in ("public", "archive"):
                    candidate = media_root / root_name / lineup_ref
                    root_dir = media_root / root_name
                    if (
                        root_dir.exists()
                        and not _has_symlink_component(root_dir)
                        and candidate.exists()
                    ):
                        _remove_test_media_tree(candidate, media_root)
                trash_root = media_root / "trash"
                if trash_root.exists() and not _has_symlink_component(trash_root):
                    for day_dir in trash_root.iterdir():
                        if day_dir.is_symlink() or not day_dir.is_dir():
                            continue
                        for root_name in ("public", "archive"):
                            candidate = day_dir / root_name / lineup_ref
                            if candidate.exists():
                                _remove_test_media_tree(candidate, media_root)


@pytest.mark.integration
def test_real_phase3_lifecycle_actions_and_metrics(
    integration_environment: str, tmp_path: Path
) -> None:
    """Exercise Phase 3 lifecycle endpoints against real PG, FFmpeg, and media roots."""

    sync_url = _sync_database_url(integration_environment)
    prefix = f"phase3-lifecycle-{uuid.uuid4().hex[:10]}"
    target_id: str | None = None
    origin_id: str | None = None
    lineup_ids: list[str] = []
    request_ids: list[str] = []
    conversation_ids: list[str] = []
    media_root = Path(os.environ["MEDIA_STORE_ROOT"])
    source_path = tmp_path / "phase3-lifecycle-input.mp4"
    _write_real_fixture(source_path)
    source_bytes = source_path.read_bytes()

    def remember(response):
        request_ids.append(response.headers["X-Request-ID"])
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("conversation_id"):
            conversation_ids.append(payload["conversation_id"])
        return payload

    def admin_json(client: TestClient, method: str, path: str, payload: object | None = None):
        kwargs: dict[str, object] = {"headers": {**_REQUEST_HEADERS, "Accept": "application/json"}}
        if payload is not None:
            kwargs["json"] = payload
        response = client.request(method, path, **kwargs)
        return response, remember(response)

    def public_json(client: TestClient, method: str, path: str, **kwargs):
        response = client.request(method, path, **kwargs)
        return response, remember(response)

    def create_lineup(client: TestClient, label: str, sort_order: int) -> str:
        response, payload = admin_json(
            client,
            "POST",
            "/api/v1/admin/lineups",
            {
                "target_id": target_id,
                "origin_id": origin_id,
                "steps": {
                    "setup": f"{label} 站位",
                    "aim": f"{label} 瞄点",
                    "throw": f"{label} 普通投掷说明",
                    "result": f"{label} 结果",
                },
                "sort_order": sort_order,
                "check_info": {"last_checked_at": None, "game_version_note": None},
            },
        )
        assert response.status_code == 201
        lineup_id = payload["id"]
        lineup_ids.append(lineup_id)
        upload = client.post(
            f"/api/v1/admin/lineups/{lineup_id}/video",
            files={"file": (f"{label}.mp4", source_bytes, "video/mp4")},
            data={"rights_note": "隔离测试自生成视频，非真实教学"},
            headers=_REQUEST_HEADERS,
        )
        uploaded = remember(upload)
        assert upload.status_code == 200
        assert uploaded["primary_video"]["status"] == "ready"
        return lineup_id

    connection = psycopg.connect(sync_url, autocommit=True)

    def seed_legacy_cache(label: str) -> None:
        connection.execute(
            """
            INSERT INTO external_search_cache
                (cache_key, payload, is_empty, expires_at, created_at)
            VALUES (%s, '[]'::jsonb, true, now() + interval '1 hour', now())
            """,
            (f"v3|{prefix}|{label}",),
        )

    def legacy_cache_count() -> int:
        row = connection.execute(
            "SELECT count(*) FROM external_search_cache WHERE cache_key LIKE %s",
            (f"v3|{prefix}|%",),
        ).fetchone()
        assert row is not None
        return int(row[0])

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            seed_legacy_cache("before-create")
            target_response, target = admin_json(
                client,
                "POST",
                "/api/v1/admin/targets",
                {
                    "slug": f"{prefix}-vip",
                    "name_zh": "VIP",
                    "name_en": "Window",
                    "description": "Phase 3 生命周期隔离目标",
                    "sort_order": 1,
                    "aliases": ["phase3-vip"],
                },
            )
            assert target_response.status_code == 201
            assert legacy_cache_count() == 1
            target_id = target["id"]
            origin_response, origin = admin_json(
                client,
                "POST",
                "/api/v1/admin/origins",
                {
                    "slug": f"{prefix}-spawn",
                    "name_zh": "T 出生点",
                    "name_en": "T Spawn",
                    "description": "Phase 3 生命周期隔离起点",
                    "sort_order": 1,
                    "aliases": ["phase3-spawn"],
                },
            )
            assert origin_response.status_code == 201
            origin_id = origin["id"]

            seed_legacy_cache("before-patch")
            target_patch, target = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}",
                {
                    "name_zh": "VIP 更新",
                    "aliases": ["phase3-vip-updated", "PHASE3 VIP UPDATED"],
                },
            )
            assert target_patch.status_code == 200
            assert target["aliases"] == ["phase3-vip-updated"]
            assert legacy_cache_count() == 2
            coordinate_patch, _ = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}",
                {"map_x": 0, "map_y": 100},
            )
            assert coordinate_patch.status_code == 422
            position_patch, target = admin_json(
                client,
                "PATCH",
                f"/api/v1/admin/targets/{target_id}/position",
                {"map_x": 0, "map_y": 100},
            )
            assert position_patch.status_code == 200
            assert (target["map_x"], target["map_y"]) == (0, 100)

            first_id = create_lineup(client, "第一条", 2)
            second_id = create_lineup(client, "第二条", 1)
            publish_first, first = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{first_id}/publish",
                {"make_default": True},
            )
            assert publish_first.status_code == 200 and first["is_default"] is True
            publish_second, second = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{second_id}/publish",
                {"make_default": False},
            )
            assert publish_second.status_code == 200 and second["is_default"] is False

            blocked_unpublish, blocked_body = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{first_id}/unpublish"
            )
            assert blocked_unpublish.status_code == 409
            assert blocked_body["error"]["code"] == "DEFAULT_REPLACEMENT_REQUIRED"
            switched, switched_body = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/set-default"
            )
            assert switched.status_code == 200 and switched_body["is_default"] is True
            unpublish, unpublished = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{first_id}/unpublish"
            )
            assert unpublish.status_code == 200 and unpublished["status"] == "draft"
            mark_needs, needs = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/mark-needs-revalidation"
            )
            assert mark_needs.status_code == 200
            assert needs["status"] == "needs_revalidation"
            assert needs["is_default"] is False
            republish, published_again = admin_json(
                client,
                "POST",
                f"/api/v1/admin/lineups/{second_id}/publish",
                {"make_default": True},
            )
            assert republish.status_code == 200 and published_again["is_default"] is True

            blocked_archive, blocked_archive_body = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/archive"
            )
            assert blocked_archive.status_code == 409
            assert blocked_archive_body["error"]["code"] == "LINEUP_STILL_PUBLISHED"
            unpublish_second, _ = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/unpublish"
            )
            assert unpublish_second.status_code == 200

            archive_video, archived_video = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/video/archive"
            )
            assert archive_video.status_code == 200
            assert archived_video["primary_video"]["status"] == "archived"
            restore_video, restored_video = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/video/restore"
            )
            assert restore_video.status_code == 200
            assert restored_video["primary_video"]["status"] == "ready"
            archive_lineup, archived_lineup = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/archive"
            )
            assert archive_lineup.status_code == 200
            assert archived_lineup["status"] == "archived"
            restore_lineup, restored_lineup = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/restore"
            )
            assert restore_lineup.status_code == 200
            assert restored_lineup["status"] == "draft"
            restore_video_again, restored_video_again = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/video/restore"
            )
            assert restore_video_again.status_code == 200
            assert restored_video_again["primary_video"]["status"] == "ready"

            archive_first, _ = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{first_id}/archive"
            )
            assert archive_first.status_code == 200
            archive_second, _ = admin_json(
                client, "POST", f"/api/v1/admin/lineups/{second_id}/archive"
            )
            assert archive_second.status_code == 200
            seed_legacy_cache("before-archive")
            archived_target, archived_target_body = admin_json(
                client, "POST", f"/api/v1/admin/targets/{target_id}/archive"
            )
            assert archived_target.status_code == 200
            assert archived_target_body["is_active"] is False
            assert legacy_cache_count() == 3
            seed_legacy_cache("before-restore")
            restored_target, _ = admin_json(
                client, "POST", f"/api/v1/admin/targets/{target_id}/restore"
            )
            assert restored_target.status_code == 200
            assert legacy_cache_count() == 4
            archived_origin, archived_origin_body = admin_json(
                client, "POST", f"/api/v1/admin/origins/{origin_id}/archive"
            )
            assert archived_origin.status_code == 200
            assert archived_origin_body["is_active"] is False
            restored_origin, _ = admin_json(
                client, "POST", f"/api/v1/admin/origins/{origin_id}/restore"
            )
            assert restored_origin.status_code == 200

            listed, listed_body = admin_json(client, "GET", "/api/v1/admin/lineups?status=all")
            assert listed.status_code == 200
            assert {item["id"] for item in listed_body} >= set(lineup_ids)
            metrics, metrics_body = admin_json(client, "GET", "/api/v1/admin/metrics/summary")
            assert metrics.status_code == 200
            assert metrics_body["window_hours"] == 24
            assert set(metrics_body["response_counts"]) == {
                "match",
                "route_matches",
                "clarification",
                "not_found",
                "out_of_scope",
            }

            map_response, map_body = public_json(client, "GET", "/api/v1/maps/mirage")
            assert map_response.status_code == 200
            assert map_body["map"]["asset_status"] == "approved"

            # The routes are archived at this point, so public requests are
            # deliberately not expected to return teaching content.  They
            # still exercise the fixed JSON error and metrics event path.
            search_response, search_body = public_json(
                client,
                "POST",
                "/api/v1/search",
                json={"kind": "query", "text": "VIP", "input_mode": "text"},
            )
            assert search_response.status_code in {200, 404, 503}
            assert isinstance(search_body, dict)
    finally:
        connection.execute(
            "DELETE FROM external_search_cache WHERE cache_key LIKE %s", (f"%{prefix}%",)
        )
        for request_id in request_ids:
            connection.execute(
                "DELETE FROM usage_events WHERE request_id = %s", (uuid.UUID(request_id),)
            )
        for conversation_id in conversation_ids:
            connection.execute(
                "DELETE FROM conversation_states WHERE id = %s", (uuid.UUID(conversation_id),)
            )
        for lineup_id in lineup_ids:
            connection.execute(
                "DELETE FROM tutorial_videos WHERE lineup_id = %s", (uuid.UUID(lineup_id),)
            )
        for lineup_id in lineup_ids:
            connection.execute("DELETE FROM lineups WHERE id = %s", (uuid.UUID(lineup_id),))
        if target_id:
            connection.execute(
                "DELETE FROM target_aliases WHERE target_id = %s", (uuid.UUID(target_id),)
            )
            connection.execute("DELETE FROM targets WHERE id = %s", (uuid.UUID(target_id),))
        if origin_id:
            connection.execute(
                "DELETE FROM origin_aliases WHERE origin_id = %s", (uuid.UUID(origin_id),)
            )
            connection.execute("DELETE FROM origins WHERE id = %s", (uuid.UUID(origin_id),))
        connection.close()
        if media_root == Path("/data/media-store") and not _has_symlink_component(media_root):
            for lineup_id in lineup_ids:
                for root_name in ("public", "archive"):
                    candidate = media_root / root_name / lineup_id
                    if (
                        candidate.exists()
                        and not candidate.is_symlink()
                        and not _has_symlink_component(candidate)
                    ):
                        _remove_test_media_tree(candidate, media_root)
                trash_root = media_root / "trash"
                if trash_root.exists() and not _has_symlink_component(trash_root):
                    for day_dir in trash_root.iterdir():
                        if day_dir.is_symlink() or not day_dir.is_dir():
                            continue
                        for root_name in ("public", "archive"):
                            candidate = day_dir / root_name / lineup_id
                            if candidate.exists():
                                _remove_test_media_tree(candidate, media_root)
