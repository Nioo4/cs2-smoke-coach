"""Environment-backed settings and fail-closed production checks."""

from __future__ import annotations

import base64
import binascii
import os
import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from argon2 import Type, extract_parameters
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigError(RuntimeError):
    """A safe configuration error whose public representation contains no values."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = tuple(dict.fromkeys(fields))
        super().__init__("invalid configuration: " + ", ".join(self.fields))


class Settings(BaseSettings):
    """Runtime settings shared by the API and operational commands."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline"
    postgres_password: SecretStr = SecretStr("smokeline")
    db_pool_size: int = 10
    db_max_overflow: int = 0
    public_base_url: str = "http://localhost:8080"
    site_domain: str = ""
    allowed_origins: str = "http://localhost:8080,http://localhost:5173"
    media_store_root: str = "/data/media-store"
    temp_root: str = "/data/tmp"
    backup_root: str = "/data/backups"
    ai_provider: Literal["disabled", "dashscope", "fake"] = "disabled"
    dashscope_api_key: SecretStr = SecretStr("")
    dashscope_api_host: str = ""
    qwen_text_model: str = "qwen3.7-flash-2026-07-15"
    qwen_asr_model: str = "qwen-audio-3.0-asr-flash"
    daily_llm_call_limit: int = 100
    daily_asr_seconds_limit: int = 600
    admin_username: str = "admin"
    admin_password_hash_b64: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def postgres_password_value(self) -> str:
        return self.postgres_password.get_secret_value()

    @property
    def dashscope_api_key_value(self) -> str:
        return self.dashscope_api_key.get_secret_value()

    def validate_runtime(self) -> Settings:
        """Validate the cross-field contract before creating engines or clients."""

        fields: list[str] = []
        if self.db_pool_size < 1:
            fields.append("DB_POOL_SIZE")
        if self.db_max_overflow < 0:
            fields.append("DB_MAX_OVERFLOW")

        try:
            database = make_url(self.database_url)
        except (ArgumentError, TypeError, ValueError):
            fields.append("DATABASE_URL")
        else:
            if (
                database.drivername != "postgresql+psycopg"
                or database.host != "postgres"
                or database.port != 5432
                or database.database != "smokeline"
                or database.username != "smokeline"
                or database.password != self.postgres_password_value
            ):
                fields.append("DATABASE_URL")
                fields.append("POSTGRES_PASSWORD")

        base_url = _parse_base_url(self.public_base_url)
        if base_url is None:
            fields.append("PUBLIC_BASE_URL")
        if self.site_domain and not _valid_domain(self.site_domain):
            fields.append("SITE_DOMAIN")
        if any("*" in origin for origin in self.cors_origins):
            fields.append("ALLOWED_ORIGINS")

        if self.app_env == "production":
            if base_url is None or base_url.scheme != "https" or base_url.port is not None:
                fields.append("PUBLIC_BASE_URL")
            if not self.site_domain or not _valid_domain(self.site_domain):
                fields.append("SITE_DOMAIN")
            elif base_url is not None and base_url.hostname != self.site_domain.lower():
                fields.append("SITE_DOMAIN")
                fields.append("PUBLIC_BASE_URL")
            if self.ai_provider == "fake":
                fields.append("AI_PROVIDER")
            if self.postgres_password_value.lower() in {"", "smokeline", "password", "postgres"}:
                fields.append("POSTGRES_PASSWORD")
            if not _valid_argon2_hash(self.admin_password_hash_b64):
                fields.append("ADMIN_PASSWORD_HASH_B64")
            for name, value in (
                ("DAILY_LLM_CALL_LIMIT", self.daily_llm_call_limit),
                ("DAILY_ASR_SECONDS_LIMIT", self.daily_asr_seconds_limit),
            ):
                if value < 1:
                    fields.append(name)
            for name, value in (
                ("MEDIA_STORE_ROOT", self.media_store_root),
                ("TEMP_ROOT", self.temp_root),
                ("BACKUP_ROOT", self.backup_root),
            ):
                if not _safe_absolute_path(value):
                    fields.append(name)
            if not _distinct_paths([self.media_store_root, self.temp_root, self.backup_root]):
                fields.extend(["MEDIA_STORE_ROOT", "TEMP_ROOT", "BACKUP_ROOT"])

        if self.ai_provider == "dashscope":
            if not self.dashscope_api_key_value:
                fields.append("DASHSCOPE_API_KEY")
            if not _valid_dashscope_host(self.dashscope_api_host):
                fields.append("DASHSCOPE_API_HOST")

        if self.ai_provider == "fake" and self.app_env != "test":
            fields.append("AI_PROVIDER")

        if fields:
            raise ConfigError(fields)
        return self


def _parse_base_url(value: str):
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is not None and not (1 <= port <= 65535):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    return parsed


_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?=^.{{1,253}}$){_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*$", re.IGNORECASE)
_DASHSCOPE_RE = re.compile(
    rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*\.cn-beijing\.maas\.aliyuncs\.com$",
    re.IGNORECASE,
)


def _valid_domain(value: str) -> bool:
    if any(char.isspace() or char in "/:@?&#%$`^*\\" for char in value):
        return False
    return bool(_DOMAIN_RE.fullmatch(value))


def _valid_dashscope_host(value: str) -> bool:
    return bool(_DASHSCOPE_RE.fullmatch(value)) and not any(
        char in "/:\\{}" or char.isspace() for char in value
    )


def _safe_absolute_path(value: str) -> bool:
    if not value or not os.path.isabs(value):
        return False
    normalized = os.path.normpath(value)
    root = os.path.abspath(os.path.sep)
    forbidden = {root, os.path.join(root, "data")}
    return normalized not in forbidden


def _distinct_paths(values: list[str]) -> bool:
    normalized = [os.path.normcase(os.path.normpath(value)) for value in values]
    if len(set(normalized)) != len(normalized):
        return False
    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            try:
                if os.path.commonpath([left, right]) in {left, right}:
                    return False
            except ValueError:
                return False
    return True


def _valid_argon2_hash(value: str) -> bool:
    if not value or any(char.isspace() for char in value):
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
        parameters = extract_parameters(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return (
        parameters.type is Type.ID
        and parameters.time_cost == 3
        and parameters.memory_cost == 65536
        and parameters.parallelism == 2
        and parameters.hash_len == 32
        and parameters.salt_len == 16
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = [
            str(item.get("loc", ("UNKNOWN",))[0]).upper()
            for item in exc.errors()
            if item.get("loc")
        ]
        raise ConfigError(fields or ["CONFIGURATION"]) from None
    return settings.validate_runtime()
