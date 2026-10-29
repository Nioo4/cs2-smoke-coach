import json
import logging
from logging.config import fileConfig
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import ConfigError
from app.scripts import start


class ExecCalled(Exception):
    pass


def test_start_runs_migration_then_replaces_process(monkeypatch) -> None:
    upgraded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        start,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg://smokeline:p%40ss@postgres:5432/smokeline"
        ),
    )
    monkeypatch.setattr(
        start.command,
        "upgrade",
        lambda config, revision: upgraded.append(
            (config.get_main_option("sqlalchemy.url"), revision)
        ),
    )

    async def no_media_reconcile() -> None:
        return None

    monkeypatch.setattr(start, "_run_media_reconcile", no_media_reconcile)

    def fake_exec(file: str, arguments: list[str]) -> None:
        assert file == "uvicorn"
        assert arguments[-2:] == ["--log-config", "/opt/smokeline/backend/uvicorn-log-config.json"]
        raise ExecCalled

    monkeypatch.setattr(start.os, "execvp", fake_exec)
    with pytest.raises(ExecCalled):
        start.main()

    assert upgraded == [("postgresql+psycopg://smokeline:p%40ss@postgres:5432/smokeline", "head")]


def test_start_rejects_invalid_configuration_without_echoing_values(monkeypatch) -> None:
    monkeypatch.setattr(
        start,
        "get_settings",
        lambda: (_ for _ in ()).throw(ConfigError(["DATABASE_URL"])),
    )
    assert start.main() == 78


def test_start_logs_only_rejected_configuration_field_names(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        start,
        "get_settings",
        lambda: (_ for _ in ()).throw(ConfigError(["DATABASE_URL", "POSTGRES_PASSWORD"])),
    )

    assert start.main() == 78
    output = capsys.readouterr().err.strip().splitlines()
    payload = json.loads(output[-1])
    assert payload["fields"] == ["DATABASE_URL", "POSTGRES_PASSWORD"]
    assert set(payload) <= {"time", "level", "event", "error_code", "fields"}
    assert "SECRET_SENTINEL" not in output[-1]


def test_start_returns_nonzero_on_migration_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        start,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://safe"),
    )
    monkeypatch.setattr(
        start.command,
        "upgrade",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("SECRET_SENTINEL")),
    )
    assert start.main() == 1


@pytest.mark.parametrize(
    ("failure_stage", "expected_event", "expected_code"),
    [
        ("migration", "migration", "MIGRATION_FAILED"),
        ("media", "media_reconcile", "MEDIA_RECONCILE_FAILED"),
    ],
)
def test_start_restores_safe_logging_after_alembic_file_config(
    monkeypatch, capsys, failure_stage, expected_event, expected_code
) -> None:
    fileConfig(str(Path(__file__).parents[1] / "alembic.ini"))
    assert logging.getLogger("smokeline").disabled is True

    monkeypatch.setattr(
        start,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://safe"),
    )
    if failure_stage == "migration":
        monkeypatch.setattr(
            start.command,
            "upgrade",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("SECRET_SENTINEL")),
        )
    else:

        async def fail_reconcile() -> None:
            raise RuntimeError("SECRET_SENTINEL")

        monkeypatch.setattr(start.command, "upgrade", lambda *_args: None)
        monkeypatch.setattr(start, "_run_media_reconcile", fail_reconcile)

    assert start.main() == 1
    lines = capsys.readouterr().err.strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["event"] == expected_event
    assert payload["error_code"] == expected_code
    assert "SECRET_SENTINEL" not in lines[-1]
    assert logging.getLogger("uvicorn.access").disabled is True
