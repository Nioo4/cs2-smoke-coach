import base64

import pytest
from argon2 import PasswordHasher

from app.core.config import ConfigError, Settings


def test_development_defaults_are_closed_over_postgres_shape(monkeypatch) -> None:
    for name in (
        "APP_ENV",
        "ALLOWED_ORIGINS",
        "AI_PROVIDER",
        "PUBLIC_BASE_URL",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings().validate_runtime()
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.cors_origins == ["http://localhost:8080", "http://localhost:5173"]


def test_database_password_must_match_decoded_url() -> None:
    decoded = Settings(
        database_url="postgresql+psycopg://smokeline:p%40ss@postgres:5432/smokeline",
        postgres_password="p@ss",
    ).validate_runtime()
    assert decoded.postgres_password_value == "p@ss"

    settings = Settings(
        database_url="postgresql+psycopg://smokeline:p%40ss@postgres:5432/smokeline",
        postgres_password="wrong",
    )
    with pytest.raises(ConfigError) as error:
        settings.validate_runtime()
    assert {"DATABASE_URL", "POSTGRES_PASSWORD"}.issubset(error.value.fields)


def test_malformed_base_url_is_reported_as_a_safe_field(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://smokeline.example:SECRET_SENTINEL")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError) as error:
            get_settings()
    finally:
        get_settings.cache_clear()
    assert "PUBLIC_BASE_URL" in error.value.fields
    assert "SECRET_SENTINEL" not in str(error.value)


def test_invalid_configuration_does_not_echo_secret() -> None:
    secret = "SECRET_SENTINEL"
    settings = Settings(
        database_url=f"postgresql+psycopg://smokeline:{secret}@postgres:5432/smokeline",
        postgres_password="different",
    )
    with pytest.raises(ConfigError) as error:
        settings.validate_runtime()
    assert secret not in str(error.value)
    assert secret not in repr(error.value)


def test_fake_provider_is_test_only() -> None:
    with pytest.raises(ConfigError) as error:
        Settings(app_env="development", ai_provider="fake").validate_runtime()
    assert "AI_PROVIDER" in error.value.fields
    assert Settings(app_env="test", ai_provider="fake").validate_runtime().ai_provider == "fake"


def test_production_rejects_defaults_and_accepts_fixed_hash_shape() -> None:
    with pytest.raises(ConfigError) as error:
        Settings(
            app_env="production",
            public_base_url="https://smokeline.example",
            site_domain="smokeline.example",
            ai_provider="disabled",
        ).validate_runtime()
    assert {
        "POSTGRES_PASSWORD",
        "ADMIN_PASSWORD_HASH_B64",
        "MEDIA_STORE_ROOT",
        "TEMP_ROOT",
        "BACKUP_ROOT",
    }.intersection(error.value.fields)

    password_hash = PasswordHasher(
        time_cost=3,
        memory_cost=65536,
        parallelism=2,
        hash_len=32,
        salt_len=16,
    ).hash("test-only-password")
    settings = Settings(
        app_env="production",
        database_url="postgresql+psycopg://smokeline:S3cure%21db@postgres:5432/smokeline",
        postgres_password="S3cure!db",
        public_base_url="https://smokeline.example",
        site_domain="smokeline.example",
        ai_provider="disabled",
        media_store_root="/srv/smokeline/media-store",
        temp_root="/srv/smokeline/tmp",
        backup_root="/srv/smokeline/backups",
        admin_password_hash_b64=base64.b64encode(password_hash.encode()).decode(),
    ).validate_runtime()
    assert settings.app_env == "production"


def test_dashscope_requires_host_and_key() -> None:
    with pytest.raises(ConfigError) as error:
        Settings(ai_provider="dashscope").validate_runtime()
    assert {"DASHSCOPE_API_KEY", "DASHSCOPE_API_HOST"}.issubset(error.value.fields)
