"""Strict internal contracts exchanged with the controlled search providers."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from app.schemas.public import SLUG_PATTERN

Slug = Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)]


class AgentModel(BaseModel):
    """Provider-facing models reject every field not in the frozen contract."""

    model_config = ConfigDict(extra="forbid")


class IntentCandidate(AgentModel):
    slug: Slug
    confidence: float

    @field_validator("confidence", mode="before")
    @classmethod
    def finite_confidence(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a JSON number")
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError("confidence must be finite and between 0 and 1")
        return number


class IntentExtraction(AgentModel):
    map: Literal["mirage", "unsupported"] | None
    grenade: Literal["smoke", "unsupported"] | None
    target_candidates: Annotated[list[IntentCandidate], Field(min_length=0, max_length=6)]
    origin_candidates: Annotated[list[IntentCandidate], Field(min_length=0, max_length=6)]
    target_mentioned: StrictBool
    origin_mentioned: StrictBool
    is_follow_up: StrictBool
    explicitly_replaced_fields: Annotated[
        list[Literal["target", "origin"]], Field(min_length=0, max_length=2)
    ]

    @field_validator("explicitly_replaced_fields")
    @classmethod
    def unique_replaced_fields(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("explicitly_replaced_fields must be unique")
        return value


class CanonicalIntent(AgentModel):
    map: Literal["mirage"]
    grenade: Literal["smoke"]
    target_slug: Slug | None
    origin_slug: Slug | None
