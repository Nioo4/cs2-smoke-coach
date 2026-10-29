"""Strict administrative API models.

The API deliberately keeps these models small.  Persistence models are not
used as request models so that a client cannot set server-owned state such as
status, ids, paths, or default flags.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.services.normalization import normalize_text

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LINE_SEPARATOR_CHARS = {"\u2028", "\u2029"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def _text(value: str, *, minimum: int, maximum: int, allow_newlines: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_text")
    if not allow_newlines and ("\n" in value or "\r" in value):
        raise ValueError("invalid_text")
    if any(
        unicodedata.category(char) == "Cc" and (not allow_newlines or char not in ("\n", "\r"))
        for char in value
    ):
        raise ValueError("invalid_text")
    if any(char in _LINE_SEPARATOR_CHARS for char in value):
        raise ValueError("invalid_text")
    length = len(value)
    if not minimum <= length <= maximum:
        raise ValueError("invalid_length")
    return value


def _slug(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not _SLUG_RE.fullmatch(value):
        raise ValueError("invalid_slug")
    return value


def _aliases(values: list[str]) -> list[str]:
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError("invalid_aliases")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        original = _text(value, minimum=1, maximum=64)
        normalized = normalize_text(original)
        if not 1 <= len(normalized) <= 64:
            raise ValueError("invalid_aliases")
        if normalized not in seen:
            seen.add(normalized)
            output.append(original)
    return output


class PointCreate(StrictModel):
    slug: StrictStr
    name_zh: StrictStr
    name_en: StrictStr | None = None
    description: StrictStr = ""
    sort_order: StrictInt = 0
    aliases: list[StrictStr] = Field(default_factory=list)

    _validate_slug = field_validator("slug")(_slug)
    _validate_name_zh = field_validator("name_zh")(
        lambda value: _text(value, minimum=1, maximum=64)
    )
    _validate_name_en = field_validator("name_en")(
        lambda value: None if value is None else _text(value, minimum=1, maximum=64)
    )
    _validate_description = field_validator("description")(
        lambda value: _text(value, minimum=0, maximum=500)
    )
    _validate_aliases = field_validator("aliases")(_aliases)

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: int) -> int:
        if not 0 <= value <= 9999:
            raise ValueError("invalid_sort_order")
        return value


class PointPatch(StrictModel):
    name_zh: StrictStr | None = None
    name_en: StrictStr | None = None
    description: StrictStr | None = None
    sort_order: StrictInt | None = None
    aliases: list[StrictStr] | None = None

    _validate_name_zh = field_validator("name_zh")(
        lambda value: None if value is None else _text(value, minimum=1, maximum=64)
    )
    _validate_name_en = field_validator("name_en")(
        lambda value: None if value is None else _text(value, minimum=1, maximum=64)
    )
    _validate_description = field_validator("description")(
        lambda value: None if value is None else _text(value, minimum=0, maximum=500)
    )
    _validate_aliases = field_validator("aliases")(
        lambda value: None if value is None else _aliases(value)
    )

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: int | None) -> int:
        if value is None or not 0 <= value <= 9999:
            raise ValueError("invalid_sort_order")
        return value

    @model_validator(mode="after")
    def validate_patch(self) -> PointPatch:
        if not self.model_fields_set:
            raise ValueError("empty_patch")
        return self


class TargetPositionPatch(StrictModel):
    map_x: float | None
    map_y: float | None

    @field_validator("map_x", "map_y", mode="before")
    @classmethod
    def validate_coordinate(cls, value: object) -> float | None:
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("invalid_coordinate")
        if isinstance(value, int):
            if not 0 <= value <= 100:
                raise ValueError("invalid_coordinate")
        elif not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("invalid_coordinate")
        return float(value)

    @model_validator(mode="after")
    def validate_pair(self) -> TargetPositionPatch:
        if (self.map_x is None) != (self.map_y is None):
            raise ValueError("coordinate_pair_required")
        return self


class PointOut(StrictModel):
    id: UUID
    slug: str
    name_zh: str
    name_en: str | None
    description: str
    map_x: float | None
    map_y: float | None
    sort_order: int
    aliases: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class Steps(StrictModel):
    setup: StrictStr
    aim: StrictStr
    throw: StrictStr
    result: StrictStr

    _validate_steps = field_validator("setup", "aim", "throw", "result")(
        lambda value: _text(value, minimum=0, maximum=1000, allow_newlines=True)
    )


class CheckInfo(StrictModel):
    last_checked_at: datetime | None = None
    game_version_note: StrictStr | None = None

    @field_validator("game_version_note")
    @classmethod
    def validate_note(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, minimum=1, maximum=128)

    @field_validator("last_checked_at")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timezone_required")
        if value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("future_time")
        return value.astimezone(UTC)


class LineupCreate(StrictModel):
    target_id: UUID
    origin_id: UUID
    steps: Steps
    sort_order: StrictInt = 0
    check_info: CheckInfo = Field(default_factory=CheckInfo)

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: int) -> int:
        if not 0 <= value <= 9999:
            raise ValueError("invalid_sort_order")
        return value


class LineupPatch(StrictModel):
    steps: Steps | None = None
    sort_order: StrictInt | None = None
    check_info: CheckInfo | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> LineupPatch:
        if not self.model_fields_set:
            raise ValueError("empty_patch")
        if "steps" in self.model_fields_set and "check_info" in self.model_fields_set:
            raise ValueError("steps_and_check_info_together")
        return self

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: int | None) -> int | None:
        if value is None or not 0 <= value <= 9999:
            raise ValueError("invalid_sort_order")
        return value


class PublishRequest(StrictModel):
    make_default: StrictBool


class AdminVideoOut(StrictModel):
    id: UUID
    status: str
    duration_ms: int | None
    size_bytes: int | None
    rights_note: str
    failure_reason: str | None
    url: str | None
    poster_url: str | None


class AdminCatalogRef(StrictModel):
    id: UUID
    slug: str
    name_zh: str


class AdminSessionOut(StrictModel):
    authenticated: StrictBool


class AdminLineupOut(StrictModel):
    id: UUID
    status: str
    is_default: bool
    default_needs_review: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    target: AdminCatalogRef
    origin: AdminCatalogRef
    steps: Steps
    check_info: CheckInfo
    primary_video: AdminVideoOut | None


class MetricResponseCounts(StrictModel):
    match: int = 0
    route_matches: int = 0
    clarification: int = 0
    not_found: int = 0
    out_of_scope: int = 0


class MetricLatency(StrictModel):
    p50: int = 0
    p95: int = 0


class MetricProviderIntent(StrictModel):
    attempts: int = 0
    success: int = 0
    failed: int = 0
    unknown: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class MetricProviderAsr(StrictModel):
    attempts: int = 0
    success: int = 0
    failed: int = 0
    unknown: int = 0
    reserved_seconds: float = 0


class MetricProviderUsage(StrictModel):
    intent: MetricProviderIntent
    asr: MetricProviderAsr


class MetricNotFoundCombination(StrictModel):
    target_slug: str
    origin_slug: str | None
    count: int


class MetricError(StrictModel):
    code: str
    count: int


class MetricsSummary(StrictModel):
    window_hours: int = 24
    from_: datetime = Field(alias="from")
    to: datetime
    response_counts: MetricResponseCounts
    latency_ms: MetricLatency
    provider_usage: MetricProviderUsage
    not_found_combinations: list[MetricNotFoundCombination]
    errors: list[MetricError]
