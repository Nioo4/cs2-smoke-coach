from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.admin import (
    LineupPatch,
    PointCreate,
    PointPatch,
    PublishRequest,
    Steps,
    TargetPositionPatch,
)
from app.schemas.public import QueryRequest


def test_query_rejects_nul_and_non_whitespace_controls() -> None:
    for value in ("VIP\x00烟", "VIP\x1f烟", "VIP\x7f烟"):
        with pytest.raises(ValidationError):
            QueryRequest(kind="query", text=value, input_mode="text")
    QueryRequest(kind="query", text="VIP\t烟\n", input_mode="text")


@pytest.mark.parametrize("separator", ["\t", "\n", "\v", "\f", "\r", "\u0085", "\u3000"])
def test_query_preserves_unicode_whitespace_and_trims_before_length(separator: str) -> None:
    body = QueryRequest(kind="query", text=separator * 201 + "VIP" + separator, input_mode="text")
    assert body.text == "VIP"
    assert QueryRequest(kind="query", text="VIP" + separator + "烟", input_mode="text").text
    with pytest.raises(ValidationError):
        QueryRequest(kind="query", text=separator * 201, input_mode="text")
    with pytest.raises(ValidationError):
        QueryRequest(kind="query", text="V" * 201, input_mode="text")


def test_admin_text_rejects_tabs_and_keeps_only_normal_newlines() -> None:
    for value in ("一\t行", "一\x00行", "一\x7f行", "一\u0085行"):
        with pytest.raises(ValidationError):
            Steps(setup=value, aim="", throw="", result="")
    Steps(setup="第一行\n第二行\r第三行", aim="", throw="", result="")


def test_lineup_patch_rejects_explicit_null_sort_order() -> None:
    with pytest.raises(ValidationError):
        LineupPatch(sort_order=None)
    assert LineupPatch(sort_order=0).sort_order == 0


def test_publish_requires_a_strict_boolean() -> None:
    assert PublishRequest(make_default=True).make_default is True
    with pytest.raises(ValidationError):
        PublishRequest(make_default=1)
    with pytest.raises(ValidationError):
        PublishRequest(make_default="true")


@pytest.mark.parametrize("model", [PointCreate, PointPatch])
def test_generic_point_models_reject_map_coordinates(model) -> None:
    payload = {"map_x": 50, "map_y": 50}
    if model is PointCreate:
        payload.update(slug="vip", name_zh="VIP")
    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("value", [True, False, "1", "50.5", "", [], [50], 101, -1])
def test_target_position_rejects_boolean_strings_and_out_of_range(value: object) -> None:
    with pytest.raises(ValidationError):
        TargetPositionPatch(map_x=value, map_y=50)


def test_target_position_requires_both_keys_and_a_complete_pair() -> None:
    assert TargetPositionPatch(map_x=None, map_y=None).model_dump() == {
        "map_x": None,
        "map_y": None,
    }
    assert TargetPositionPatch(map_x=0, map_y=100).model_dump() == {"map_x": 0.0, "map_y": 100.0}
    for payload in (
        {"map_x": None, "map_y": 1},
        {"map_x": 1, "map_y": None},
        {"map_x": 1},
        {"map_y": 1},
        {"map_x": 1, "map_y": 1, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            TargetPositionPatch(**payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_target_position_rejects_non_finite_or_huge_numbers_without_overflow(value: object) -> None:
    with pytest.raises(ValidationError):
        TargetPositionPatch(map_x=value, map_y=1)
