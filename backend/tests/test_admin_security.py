from __future__ import annotations

import asyncio
import base64
import io
import threading
import uuid
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request

from app.api import admin
from app.core.errors import AppError


def _basic_request(username: str, password: str) -> Request:
    payload = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/admin/session",
            "raw_path": b"/api/v1/admin/session",
            "query_string": b"",
            "headers": [(b"authorization", f"Basic {payload}".encode("ascii"))],
        }
    )


def _settings(username: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(admin_username=username, admin_password_hash_b64="test-hash")


@pytest.mark.asyncio
async def test_basic_username_comparison_uses_utf8_bytes(monkeypatch) -> None:
    monkeypatch.setattr(admin, "get_settings", lambda: _settings("管理员"))
    monkeypatch.setattr(admin, "_verify_password", lambda _hash, _password: True)
    assert await admin.require_admin(_basic_request("管理员", "公开测试密码")) == "管理员"


@pytest.mark.asyncio
async def test_cancelled_argon2_waits_for_worker_before_releasing_slot(monkeypatch) -> None:
    monkeypatch.setattr(admin, "get_settings", lambda: _settings())
    started = threading.Event()
    release = threading.Event()

    def blocking_verify(_hash: str, _password: str) -> bool:
        started.set()
        while not release.wait(0.01):
            pass
        return True

    monkeypatch.setattr(admin, "_verify_password", blocking_verify)
    operation = asyncio.create_task(admin.require_admin(_basic_request("admin", "secret")))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        operation.cancel()
        await asyncio.sleep(0.05)
        assert not operation.done()
        assert admin._AUTH_SLOTS._value == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert admin._AUTH_SLOTS._value == 2
    finally:
        release.set()
        if not operation.done():
            try:
                await operation
            except BaseException:
                pass


class _Form:
    def __init__(self, items):
        self.items = items
        self.closed = False

    def multi_items(self):
        return list(self.items)

    async def close(self):
        self.closed = True
        for _key, value in self.items:
            if isinstance(value, UploadFile):
                await value.close()


class _UploadRequest:
    def __init__(self, form: _Form):
        self.form_data = form
        self.form_calls: list[dict[str, object]] = []
        self.headers = {"content-type": "multipart/form-data; boundary=test"}
        self.state = SimpleNamespace(request_id="a" * 32)

    async def form(self, **kwargs):
        self.form_calls.append(kwargs)
        return self.form_data


class _ParserFailureRequest:
    def __init__(self):
        self.form_calls: list[dict[str, object]] = []
        self.headers = {"content-type": "multipart/form-data; boundary=test"}
        self.state = SimpleNamespace(request_id="b" * 32)

    async def form(self, **kwargs):
        self.form_calls.append(kwargs)
        raise MultiPartException("parser detail is not part of the API contract")


class _MissingLineupSession:
    async def get(self, _model, _lineup_id):
        return None


@pytest.mark.asyncio
async def test_upload_form_is_bounded_and_closed_on_early_error() -> None:
    upload = UploadFile(
        io.BytesIO(b"test"),
        filename="client-name.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )
    form = _Form([("file", upload), ("rights_note", "自动化测试夹具，非真实教学")])
    request = _UploadRequest(form)
    with pytest.raises(AppError) as error:
        await admin.upload_video(
            uuid.uuid4(), request, _username="admin", session=_MissingLineupSession()
        )
    assert error.value.code == "ADMIN_LINEUP_NOT_FOUND"
    assert request.form_calls == [{"max_files": 1, "max_fields": 1}]
    assert form.closed
    assert upload.file.closed


@pytest.mark.asyncio
async def test_upload_parser_failure_is_a_validation_error_without_form_close_attribute_error() -> (
    None
):
    request = _ParserFailureRequest()
    with pytest.raises(AppError) as error:
        await admin.upload_video(
            uuid.uuid4(), request, _username="admin", session=_MissingLineupSession()
        )
    assert error.value.code == "VALIDATION_ERROR"
    assert error.value.details == {"fields": [{"path": "file", "reason": "too_many_parts"}]}
    assert request.form_calls == [{"max_files": 1, "max_fields": 1}]


@pytest.mark.asyncio
async def test_upload_rejects_rights_note_controls_and_closes_upload() -> None:
    upload = UploadFile(
        io.BytesIO(b"test"),
        filename="client-name.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )
    form = _Form([("file", upload), ("rights_note", "公开说明\x00")])
    request = _UploadRequest(form)
    with pytest.raises(AppError) as error:
        await admin.upload_video(
            uuid.uuid4(), request, _username="admin", session=_MissingLineupSession()
        )
    assert error.value.code == "VALIDATION_ERROR"
    assert error.value.details == {"fields": [{"path": "rights_note", "reason": "invalid_text"}]}
    assert form.closed
    assert upload.file.closed


@pytest.mark.asyncio
async def test_upload_rejects_multiple_files_and_closes_every_upload() -> None:
    first = UploadFile(
        io.BytesIO(b"first"),
        filename="first.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )
    second = UploadFile(
        io.BytesIO(b"second"),
        filename="second.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )
    form = _Form(
        [
            ("file", first),
            ("file", second),
            ("rights_note", "公开说明"),
        ]
    )
    request = _UploadRequest(form)
    with pytest.raises(AppError) as error:
        await admin.upload_video(
            uuid.uuid4(), request, _username="admin", session=_MissingLineupSession()
        )
    assert error.value.details == {"fields": [{"path": "file", "reason": "too_many"}]}
    assert form.closed
    assert first.file.closed and second.file.closed
