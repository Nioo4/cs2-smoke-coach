import base64

import pytest
from argon2 import PasswordHasher

from app.scripts import create_admin_hash


def test_create_hash_returns_verifiable_argon2id_value(monkeypatch) -> None:
    monkeypatch.setattr(create_admin_hash.getpass, "getpass", lambda _prompt: "test-password")
    encoded = create_admin_hash.create_hash()
    value = base64.b64decode(encoded).decode("utf-8")
    assert PasswordHasher().verify(value, "test-password")


@pytest.mark.parametrize("answers", [("", ""), ("one", "two")])
def test_create_hash_rejects_empty_or_mismatched_password(monkeypatch, answers) -> None:
    iterator = iter(answers)
    monkeypatch.setattr(create_admin_hash.getpass, "getpass", lambda _prompt: next(iterator))
    with pytest.raises(ValueError):
        create_admin_hash.create_hash()


def test_main_reports_cancel_and_validation_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        create_admin_hash,
        "create_hash",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    assert create_admin_hash.main() == 130
    assert "已取消" in capsys.readouterr().err

    monkeypatch.setattr(
        create_admin_hash,
        "create_hash",
        lambda: (_ for _ in ()).throw(ValueError("密码错误")),
    )
    assert create_admin_hash.main() == 1
    assert "密码错误" in capsys.readouterr().err


def test_main_prints_only_assignment_on_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr(create_admin_hash, "create_hash", lambda: "encoded-value")
    assert create_admin_hash.main() == 0
    assert capsys.readouterr().out == "ADMIN_PASSWORD_HASH_B64=encoded-value\n"
