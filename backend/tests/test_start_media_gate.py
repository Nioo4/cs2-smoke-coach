"""Startup must not serve HTTP before the locked media gate succeeds."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.scripts import reconcile_media, start
from app.services import media


class _ProcessReplaced(Exception):
    pass


def _startup_dependencies(monkeypatch, calls):
    monkeypatch.setattr(
        start,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://test:only@postgres:5432/test"),
    )

    def migrate(_config, revision):
        assert revision == "head"
        calls.append("migrate")

    def replace(executable, arguments):
        assert executable == "uvicorn"
        assert arguments[1] == "app.main:app"
        calls.append("exec")
        raise _ProcessReplaced

    monkeypatch.setattr(start.command, "upgrade", migrate)
    monkeypatch.setattr(start.os, "execvp", replace)


def test_startup_holds_media_lock_until_reconcile_finishes_before_exec(monkeypatch):
    calls = []
    _startup_dependencies(monkeypatch, calls)

    @asynccontextmanager
    async def lock(*, timeout_seconds):
        assert timeout_seconds == 120
        calls.append("lock")
        try:
            yield
        finally:
            calls.append("unlock")

    async def reconcile():
        assert calls == ["migrate", "lock"]
        calls.append("reconcile")

    monkeypatch.setattr(media, "media_lock", lock)
    monkeypatch.setattr(reconcile_media, "reconcile_media", reconcile)
    with pytest.raises(_ProcessReplaced):
        start.main()
    assert calls == ["migrate", "lock", "reconcile", "unlock", "exec"]


@pytest.mark.parametrize("failure_stage", ["lock", "reconcile"])
def test_media_gate_failure_stops_startup_without_leaking_exception(
    monkeypatch, capsys, failure_stage
):
    calls = []
    _startup_dependencies(monkeypatch, calls)

    @asynccontextmanager
    async def lock(*, timeout_seconds):
        assert timeout_seconds == 120
        calls.append("lock")
        if failure_stage == "lock":
            raise media.MediaLockTimeout("SECRET_MEDIA_PATH_SENTINEL")
        try:
            yield
        finally:
            calls.append("unlock")

    async def reconcile():
        calls.append("reconcile")
        raise RuntimeError("SECRET_MEDIA_PATH_SENTINEL")

    monkeypatch.setattr(media, "media_lock", lock)
    monkeypatch.setattr(reconcile_media, "reconcile_media", reconcile)
    assert start.main() == 1
    expected = ["migrate", "lock"]
    if failure_stage == "reconcile":
        expected += ["reconcile", "unlock"]
    assert calls == expected
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "MEDIA_RECONCILE_FAILED" in output
    assert "SECRET_MEDIA_PATH_SENTINEL" not in output
    assert "Traceback" not in output


@pytest.mark.asyncio
async def test_standalone_reconcile_caller_uses_same_120_second_lock(monkeypatch):
    calls = []

    @asynccontextmanager
    async def lock(*, timeout_seconds):
        assert timeout_seconds == 120
        calls.append("lock")
        try:
            yield
        finally:
            calls.append("unlock")

    async def reconcile():
        assert calls == ["lock"]
        calls.append("reconcile")

    monkeypatch.setattr(reconcile_media, "media_lock", lock)
    monkeypatch.setattr(reconcile_media, "reconcile_media", reconcile)
    await reconcile_media._run_reconcile_with_lock()
    assert calls == ["lock", "reconcile", "unlock"]
