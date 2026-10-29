"""Shared public response and search request contracts."""

from __future__ import annotations

import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.schemas.admin import Steps

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
Slug = Annotated[str, Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PointSummary(PublicModel):
    slug: Slug
    name_zh: str
    map_x: float | None
    map_y: float | None


class PrimaryVideo(PublicModel):
    url: str
    poster_url: str
    duration_ms: Annotated[int, Field(ge=1, le=20000)]


class LineupSummary(PublicModel):
    id: UUID
    is_default: bool
    variant_hint: str
    primary_video: PrimaryVideo


class OriginChoice(PublicModel):
    origin: PointSummary
    lineup: LineupSummary


class TargetChoices(PublicModel):
    target: PointSummary
    origins: Annotated[list[OriginChoice], Field(min_length=1)]


class Intent(PublicModel):
    map: Literal["mirage"] = "mirage"
    grenade: Literal["smoke"] = "smoke"
    target_slug: Slug | None
    origin_slug: Slug | None


class Notice(PublicModel):
    code: Literal["ORIGIN_UNRESOLVED"]
    message: str


Notices = Annotated[list[Notice], Field(max_length=1)]


class CheckInfo(PublicModel):
    last_checked_at: str | None
    game_version_note: str | None


class LineupDetail(PublicModel):
    id: UUID
    target: PointSummary
    origin: PointSummary
    is_default: bool
    source: Literal["local_curated"] = "local_curated"
    steps: Steps
    check_info: CheckInfo
    primary_video: PrimaryVideo
    other_lineups: list[LineupSummary]


class MapAsset(PublicModel):
    slug: Literal["mirage"]
    name_zh: str
    asset_url: str
    asset_status: Literal["placeholder", "approved"]


class MapTarget(PublicModel):
    slug: str
    name_zh: str
    map_x: float | None
    map_y: float | None
    origin_count: int


class MapResponse(PublicModel):
    map: MapAsset
    published_lineup_count: int
    targets: list[MapTarget]


class QueryRequest(PublicModel):
    kind: Literal["query"]
    text: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    conversation_id: UUID | None = None
    input_mode: Literal["text", "voice"]

    @field_validator("text", mode="before")
    @classmethod
    def non_blank(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        # Unicode White_Space control characters; the C0 file/group/record
        # separators are controls, not user whitespace even though isspace()
        # also classifies them as such in Python.
        if any(
            unicodedata.category(char) == "Cc" and char not in "\t\n\v\f\r\u0085" for char in value
        ):
            raise ValueError("invalid_text")
        value = value.strip()
        if not value:
            raise ValueError("empty_text")
        return value


class TranscriptionResponse(PublicModel):
    text: Annotated[str, Field(min_length=1, max_length=200)]
    duration_seconds: float = Field(ge=0, le=15)


class Selection(PublicModel):
    field: Literal["target"]
    value: Slug


class SelectionRequest(PublicModel):
    kind: Literal["selection"]
    conversation_id: UUID
    selection: Selection


SearchRequest = Annotated[QueryRequest | SelectionRequest, Field(discriminator="kind")]


class SearchMatch(PublicModel):
    type: Literal["match"]
    conversation_id: UUID
    source: Literal["local_curated"]
    confidence: Annotated[float, Field(ge=0, le=1)]
    intent: Intent
    notices: Notices
    lineup: LineupDetail


class RouteChoice(PublicModel):
    target: PointSummary
    origin: PointSummary
    lineup: LineupSummary


class SearchRouteMatches(PublicModel):
    type: Literal["route_matches"]
    conversation_id: UUID
    source: Literal["local_curated"]
    intent: Intent
    notices: Notices
    routes: Annotated[list[RouteChoice], Field(min_length=1)]


class ClarificationOption(PublicModel):
    value: Slug
    label: str


class SearchClarification(PublicModel):
    type: Literal["clarification"]
    conversation_id: UUID
    question: Literal["你想封哪个目标位置？"]
    field: Literal["target"]
    options: Annotated[list[ClarificationOption], Field(min_length=1, max_length=6)]


class SearchNotFound(PublicModel):
    type: Literal["not_found"]
    conversation_id: UUID
    reason: Literal["NO_TARGET", "NO_LOCAL_RESULT"]
    message: str


class SupportedScope(PublicModel):
    maps: tuple[Literal["mirage"]]
    grenades: tuple[Literal["smoke"]]


class SearchOutOfScope(PublicModel):
    type: Literal["out_of_scope"]
    conversation_id: UUID
    message: Literal["当前版本只支持 Mirage 的烟雾弹教学。"]
    supported_scope: SupportedScope


SearchResponse = Annotated[
    SearchMatch | SearchRouteMatches | SearchClarification | SearchNotFound | SearchOutOfScope,
    Field(discriminator="type"),
]
