"""Public tactic directory request bodies; document writes remain Node commands."""

from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints, field_validator

from app.schemas.tactics_auth import EmptyBody


class CreateTacticBody(EmptyBody):
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=100)]
    side: Literal["T", "CT"]

    @field_validator("title")
    @classmethod
    def title_is_plain(cls, value):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid title")
        value.encode("utf-8")
        return value


class ArchiveTacticBody(EmptyBody):
    expected_revision: Annotated[StrictInt, Field(ge=0, le=9007199254740991)]
