"""Strict transport validation of the trusted Node projection; Yjs semantics stay in Node."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Id = Annotated[str, StringConstraints(strict=True, pattern=(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
))]
Version = Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
Coordinate = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Point(Strict):
    x: Coordinate
    y: Coordinate


class Position(Point):
    version: Version


class Role(Strict):
    id: Id
    number: Annotated[int, Field(ge=1, le=5)]
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class Action(Strict):
    id: Id
    group_id: Id
    version: Version


class Move(Action):
    kind: Literal["move"]
    role_id: Id
    path: Annotated[list[Point], Field(min_length=2, max_length=50)]


class Wait(Action):
    kind: Literal["wait"]
    role_id: Id


class Observe(Action):
    kind: Literal["observe"]
    role_id: Id
    target: Point


class Utility(Action):
    kind: Literal["utility"]
    thrower_role_id: Id
    utility_type: Literal["smoke", "flash", "molotov", "he", "decoy"]
    target: Point


class Text(Action):
    kind: Literal["text"]
    role_id: Id | None
    position: Point


AnyAction = Annotated[Move | Wait | Observe | Utility | Text, Field(discriminator="kind")]


class Stage(Strict):
    id: Id
    version: Version
    initial_positions: dict[Id, Position]
    actions: Annotated[list[AnyAction], Field(max_length=100)]
    lineup_ids: Annotated[list[Id], Field(max_length=10)]


class Structure(Strict):
    schema_kind: Literal["stage-board"]
    schema_version: Literal[2]
    document_id: Id
    map: Literal["mirage"]
    side: Literal["T", "CT"]
    roles: Annotated[list[Role], Field(min_length=1, max_length=5)]
    stages: Annotated[list[Stage], Field(min_length=1, max_length=20)]


def validate_projection(projection: dict, document_id: str) -> None:
    if set(projection) != {"structure", "fields"}:
        raise ValueError("projection fields")
    structure = Structure.model_validate(projection["structure"])
    if structure.document_id != document_id:
        raise ValueError("document identity")
    ids = set()
    for entity in [*structure.roles, *structure.stages,
                   *(action for stage in structure.stages for action in stage.actions)]:
        if entity.id in ids:
            raise ValueError("identity reuse")
        ids.add(entity.id)
    role_ids = {role.id for role in structure.roles}
    if len({role.number for role in structure.roles}) != len(role_ids):
        raise ValueError("role number reuse")
    expected = {"tactic.title", "tactic.purpose", "tactic.applicability"}
    expected.update(f"role.{role.id}.name" for role in structure.roles)
    for stage in structure.stages:
        if set(stage.initial_positions) != role_ids or (
            len(set(stage.lineup_ids)) != len(stage.lineup_ids)
        ):
            raise ValueError("stage membership")
        expected.update(f"stage.{stage.id}.{name}" for name in ["name", "description", "condition"])
        positions = {key: (point.x, point.y) for key, point in stage.initial_positions.items()}
        seen_groups, used_roles = set(), set()
        current_group = None
        group_start = positions.copy()
        for action in stage.actions:
            expected.add(f"action.{action.id}.description")
            if action.group_id != current_group:
                if action.group_id in seen_groups:
                    raise ValueError("noncontiguous group")
                seen_groups.add(action.group_id)
                current_group, used_roles, group_start = action.group_id, set(), positions.copy()
            actor = action.thrower_role_id if isinstance(action, Utility) else action.role_id
            if actor is not None and actor not in role_ids:
                raise ValueError("unknown role")
            if not isinstance(action, Text):
                if actor in used_roles:
                    raise ValueError("same role group conflict")
                used_roles.add(actor)
            if isinstance(action, Move):
                start = action.path[0]
                if abs(start.x - group_start[actor][0]) >= 1e-6 or (
                    abs(start.y - group_start[actor][1]) >= 1e-6
                ):
                    raise ValueError("move start mismatch")
                positions[actor] = (action.path[-1].x, action.path[-1].y)
    fields = projection["fields"]
    if not isinstance(fields, dict) or set(fields) != expected:
        raise ValueError("text identities")
    total = 0
    for key, value in fields.items():
        if not isinstance(value, str) or len(value) > (100 if key == "tactic.title" else 2000):
            raise ValueError("text limit")
        total += len(value.encode("utf-8"))
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value) or (
            (key == "tactic.title" or key.endswith(".name") or key.startswith("action."))
            and not value.strip()
        ):
            raise ValueError("invalid text")
    if total > 1048576:
        raise ValueError("text bytes")
