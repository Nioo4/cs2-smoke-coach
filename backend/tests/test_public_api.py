from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.api import public
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import LineupStatus, VideoStatus
from app.main import app
from app.schemas.agent import IntentExtraction
from app.schemas.public import SelectionRequest
from app.services.agent_graph import (
    ConversationSnapshot,
    PendingClearResult,
    clear_pending_state,
    run_search,
)
from app.services.local_search import CatalogEntity, PublicRow, available_rows
from app.services.normalization import catalog_terms
from app.services.providers import DisabledIntentParser, ProviderSet


def _request(method: str = "POST", path: str = "/api/v1/search") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("localhost", 80),
        }
    )


def _catalog(
    slug: str,
    name: str,
    *,
    sort_order: int = 0,
    name_en: str | None = None,
    aliases: list[str] | None = None,
) -> CatalogEntity:
    return CatalogEntity(
        uuid.uuid4(),
        slug,
        name,
        name_en,
        catalog_terms(name, name_en, aliases or []),
        sort_order,
    )


def _row(
    target: CatalogEntity,
    origin: CatalogEntity,
    *,
    default: bool = True,
    sort_order: int = 0,
    setup_text: str = "站到箱子旁",
    aim_text: str = "瞄准墙角",
    throw_text: str = "跳投",
    result_text: str = "封住窗口",
) -> PublicRow:
    row_target = SimpleNamespace(
        id=target.id,
        slug=target.slug,
        name_zh=target.name_zh,
        map_x=50,
        map_y=25,
        sort_order=target.sort_order,
        is_active=True,
    )
    row_origin = SimpleNamespace(
        id=origin.id,
        slug=origin.slug,
        name_zh=origin.name_zh,
        map_x=20,
        map_y=80,
        sort_order=origin.sort_order,
        is_active=True,
    )
    lineup = SimpleNamespace(
        id=uuid.uuid4(),
        target_id=row_target.id,
        origin_id=row_origin.id,
        status=LineupStatus.published,
        is_default=default,
        sort_order=sort_order,
        setup_text=setup_text,
        aim_text=aim_text,
        throw_text=throw_text,
        result_text=result_text,
        last_checked_at=None,
        game_version_note=None,
    )
    video = SimpleNamespace(
        id=uuid.uuid4(),
        status=VideoStatus.ready,
        storage_key=uuid.uuid4(),
        relative_path=f"{lineup.id}/primary-{uuid.uuid4()}.mp4",
        poster_relative_path=f"{lineup.id}/primary-{uuid.uuid4()}.webp",
        duration_ms=2_000,
        checksum_sha256="b" * 64,
    )
    return PublicRow(lineup, row_target, row_origin, video)


def _patch_query_dependencies(monkeypatch, targets, origins, rows):
    async def catalogs(_session):
        return targets, origins

    async def available(_session):
        return rows

    async def no_save(*_args, **_kwargs):
        return None

    monkeypatch.setattr(public, "available_rows", available)

    @asynccontextmanager
    async def snapshot():
        yield object()

    monkeypatch.setattr(public, "public_snapshot", snapshot)

    monkeypatch.setitem(
        _QUERY_DEPENDENCIES,
        "value",
        {
            "catalog_loader": catalogs,
            "rows_loader": available,
            "snapshot_factory": snapshot,
            "save_conversation": no_save,
        },
    )


_QUERY_DEPENDENCIES: dict[str, object] = {"value": {}}


def _test_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        postgres_password="smokeline",
        ai_provider="disabled",
    )


async def _run_query(request: Request, body: object, **overrides):
    dependencies = _QUERY_DEPENDENCIES["value"]
    assert isinstance(dependencies, dict)

    async def no_clear(conversation_id):
        return PendingClearResult(conversation_id, None)

    async def no_load(_conversation_id, _delete_expired=True):
        return None

    async def no_save(*_args, **_kwargs):
        return None

    return await run_search(
        request,
        body,
        settings=_test_settings(),
        providers=overrides.get("providers", ProviderSet(DisabledIntentParser())),
        clear_pending=overrides.get("clear_pending", no_clear),
        load_conversation=overrides.get("load_conversation", no_load),
        save_conversation=overrides.get("save_conversation", dependencies["save_conversation"]),
        catalog_loader=dependencies["catalog_loader"],
        rows_loader=dependencies["rows_loader"],
        snapshot_factory=dependencies["snapshot_factory"],
    )


@pytest.mark.asyncio
async def test_search_out_of_scope_and_unknown_target_are_deterministic(monkeypatch) -> None:
    target = _catalog("vip", "VIP")
    _patch_query_dependencies(monkeypatch, [target], [], [])

    out_of_scope = await _run_query(
        _request(), SimpleNamespace(conversation_id=None, text="我要投 flash", input_mode="text")
    )
    assert out_of_scope["type"] == "out_of_scope"
    assert out_of_scope["supported_scope"] == {"maps": ["mirage"], "grenades": ["smoke"]}

    unknown_target = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="封不存在的点", input_mode="text"),
    )
    assert unknown_target["type"] == "not_found"
    assert unknown_target["reason"] == "NO_TARGET"


@pytest.mark.asyncio
async def test_query_clears_existing_pending_before_scope_and_dependency_failures(
    monkeypatch,
) -> None:
    conversation_id = uuid.uuid4()
    cleared: list[uuid.UUID] = []

    async def clear(value):
        cleared.append(value)
        return value

    _patch_query_dependencies(monkeypatch, [], [], [])
    body = SimpleNamespace(conversation_id=conversation_id, text="我要投 flash", input_mode="text")
    response = await _run_query(_request(), body, clear_pending=clear)
    assert response["type"] == "out_of_scope"
    assert cleared == [conversation_id]

    async def unavailable(_session):
        raise AppError(503, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试。")

    async def snapshot():
        yield object()

    body = SimpleNamespace(conversation_id=conversation_id, text="我想封 VIP", input_mode="text")

    async def no_save(*_args, **_kwargs):
        return None

    monkeypatch.setitem(
        _QUERY_DEPENDENCIES,
        "value",
        {
            "catalog_loader": unavailable,
            "rows_loader": lambda _session: [],
            "snapshot_factory": lambda: asynccontextmanager(snapshot)(),
            "save_conversation": no_save,
        },
    )
    with pytest.raises(AppError) as error:
        await _run_query(_request(), body, clear_pending=clear)
    assert error.value.code == "DATABASE_UNAVAILABLE"
    assert cleared == [conversation_id, conversation_id]


@pytest.mark.asyncio
async def test_target_ambiguity_uses_catalog_sort_order(monkeypatch) -> None:
    late = _catalog("late", "A 点", sort_order=20)
    early = _catalog("early", "Z 点", sort_order=10)
    _patch_query_dependencies(monkeypatch, [late, early], [], [])
    response = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="A 点 Z 点", input_mode="text"),
    )
    assert response["type"] == "clarification"
    assert [item["value"] for item in response["options"]] == ["early", "late"]


@pytest.mark.asyncio
async def test_follow_up_target_clarification_inherits_active_origin(monkeypatch) -> None:
    vip = _catalog("vip", "VIP", aliases=["窗口"])
    ct = _catalog("ct", "CT", aliases=["窗口"], sort_order=10)
    origin = _catalog("t-spawn", "T 出生点")
    _patch_query_dependencies(monkeypatch, [vip, ct], [origin], [_row(vip, origin)])
    conversation_id = uuid.uuid4()
    saved: dict[str, object] = {}

    async def clear(_conversation_id):
        return PendingClearResult(
            conversation_id,
            ConversationSnapshot(
                "vip",
                "t-spawn",
                None,
                None,
                datetime.now(UTC) + timedelta(minutes=5),
            ),
        )

    async def save(_conversation_id, **values):
        saved.update(values)

    response = await _run_query(
        _request(),
        SimpleNamespace(
            conversation_id=conversation_id,
            text="那改成窗口烟",
            input_mode="text",
        ),
        clear_pending=clear,
        save_conversation=save,
    )
    assert response["type"] == "clarification"
    assert [item["value"] for item in response["options"]] == ["vip", "ct"]
    assert saved == {
        "target_slug": None,
        "origin_slug": "t-spawn",
        "pending_options": ["vip", "ct"],
    }


@pytest.mark.asyncio
async def test_clarification_keeps_only_this_requests_known_origin(monkeypatch) -> None:
    vip = _catalog("vip", "VIP")
    ct = _catalog("ct", "CT")
    origin = _catalog("t-spawn", "T 出生点")
    rows = [_row(vip, origin)]
    _patch_query_dependencies(monkeypatch, [vip, ct], [origin], rows)
    saved = {}

    async def save(conversation_id, **values):
        saved.update(values, id=conversation_id)

    async def load(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            saved.get("target_slug"),
            saved.get("origin_slug"),
            "target",
            saved.get("pending_options"),
            datetime.now(UTC) + timedelta(minutes=5),
        )

    async def clear(conversation_id):
        return conversation_id

    first = await _run_query(
        _request(),
        SimpleNamespace(
            conversation_id=None, text="从 T 出生点跳投封 VIP 或 CT", input_mode="text"
        ),
        save_conversation=save,
        load_conversation=load,
        clear_pending=clear,
    )
    assert first["type"] == "clarification"
    assert saved["origin_slug"] == "t-spawn"
    assert saved["target_slug"] is None
    selection = SelectionRequest(
        kind="selection",
        conversation_id=uuid.UUID(first["conversation_id"]),
        selection={"field": "target", "value": "vip"},
    )
    chosen = await _run_query(
        _request(),
        selection,
        save_conversation=save,
        load_conversation=load,
        clear_pending=clear,
    )
    assert chosen["type"] == "match"
    assert chosen["intent"]["origin_slug"] == "t-spawn"
    assert chosen["notices"] == []

    fresh_query = await _run_query(
        _request(),
        SimpleNamespace(
            conversation_id=selection.conversation_id,
            text="我想封 VIP",
            input_mode="text",
        ),
        save_conversation=save,
        load_conversation=load,
        clear_pending=clear,
    )
    assert fresh_query["type"] == "route_matches"
    assert fresh_query["intent"]["origin_slug"] is None
    assert "throw_method" not in fresh_query["routes"][0]["lineup"]


@pytest.mark.asyncio
async def test_pending_origin_cross_field_ambiguity_is_not_saved_as_explicit(monkeypatch) -> None:
    vip = _catalog("vip", "VIP")
    ct = _catalog("ct", "CT")
    origin = _catalog("vip-platform", "VIP")
    _patch_query_dependencies(monkeypatch, [vip, ct], [origin], [])
    saved = {}

    class ControlledIntent:
        async def parse(self, _text, _catalog, _previous_intent):
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[
                    {"slug": "vip", "confidence": 0.80},
                    {"slug": "ct", "confidence": 0.79},
                ],
                origin_candidates=[],
                target_mentioned=True,
                origin_mentioned=False,
                is_follow_up=False,
                explicitly_replaced_fields=[],
            )

    async def save(_conversation_id, **values):
        saved.update(values)

    response = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="VIP 或 CT", input_mode="text"),
        save_conversation=save,
        providers=ProviderSet(ControlledIntent()),
    )
    assert response["type"] == "clarification"
    assert saved["origin_slug"] is None


@pytest.mark.asyncio
async def test_llm_target_clarification_persists_unique_origin_for_selection(monkeypatch) -> None:
    vip = _catalog("vip", "VIP", aliases=["窗口"])
    ct = _catalog("ct", "CT", aliases=["窗口"])
    origin = _catalog("t-spawn", "T 出生点", aliases=["窗口"])
    _patch_query_dependencies(monkeypatch, [vip, ct], [origin], [_row(vip, origin)])
    saved: dict[str, object] = {}

    class ControlledIntent:
        async def parse(self, _text, _catalog, _previous_intent):
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[
                    {"slug": "vip", "confidence": 0.80},
                    {"slug": "ct", "confidence": 0.79},
                ],
                origin_candidates=[{"slug": "t-spawn", "confidence": 0.96}],
                target_mentioned=True,
                origin_mentioned=True,
                is_follow_up=False,
                explicitly_replaced_fields=[],
            )

    async def save(_conversation_id, **values):
        saved.update(values)

    async def load(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            saved.get("target_slug"),
            saved.get("origin_slug"),
            "target" if saved.get("pending_options") else None,
            saved.get("pending_options"),
            datetime.now(UTC) + timedelta(minutes=5),
        )

    providers = ProviderSet(ControlledIntent())
    first = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="窗口烟", input_mode="text"),
        providers=providers,
        save_conversation=save,
        load_conversation=load,
    )
    assert first["type"] == "clarification"
    assert saved["origin_slug"] == "t-spawn"

    selected = await _run_query(
        _request(),
        SelectionRequest(
            kind="selection",
            conversation_id=uuid.UUID(first["conversation_id"]),
            selection={"field": "target", "value": "vip"},
        ),
        providers=providers,
        save_conversation=save,
        load_conversation=load,
    )
    assert selected["type"] == "match"
    assert selected["intent"]["origin_slug"] == "t-spawn"
    assert saved["pending_options"] is None


@pytest.mark.asyncio
async def test_llm_clarification_rechecks_active_catalog_before_pending(monkeypatch) -> None:
    vip = _catalog("vip", "VIP", aliases=["模糊目标"])
    ct = _catalog("ct", "CT", aliases=["模糊目标"])
    origin = _catalog("t-spawn", "T 出生点")
    _patch_query_dependencies(monkeypatch, [vip, ct], [origin], [])
    calls = 0

    async def catalogs(_session):
        nonlocal calls
        calls += 1
        # The first snapshot is the catalog supplied to Intent.  During that
        # call CT is archived, so only VIP may remain selectable.
        return ([vip, ct] if calls == 1 else [vip]), [origin]

    dependencies = _QUERY_DEPENDENCIES["value"]
    assert isinstance(dependencies, dict)
    dependencies["catalog_loader"] = catalogs
    saved: dict[str, object] = {}

    class ControlledIntent:
        async def parse(self, _text, _catalog, _previous_intent):
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[
                    {"slug": "vip", "confidence": 0.80},
                    {"slug": "ct", "confidence": 0.79},
                ],
                origin_candidates=[],
                target_mentioned=True,
                origin_mentioned=False,
                is_follow_up=False,
                explicitly_replaced_fields=[],
            )

    async def save(_conversation_id, **values):
        saved.update(values)

    response = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="模糊位置", input_mode="text"),
        providers=ProviderSet(ControlledIntent()),
        save_conversation=save,
    )
    assert response["type"] == "clarification"
    assert response["options"] == [{"value": "vip", "label": "VIP"}]
    assert saved == {"target_slug": None, "origin_slug": None, "pending_options": ["vip"]}
    assert calls == 2


@pytest.mark.asyncio
async def test_selection_does_not_substitute_a_different_origin(monkeypatch) -> None:
    target = _catalog("vip", "VIP")
    requested = _catalog("t-spawn", "T 出生点")
    available = _catalog("a-ramp", "A1")
    _patch_query_dependencies(
        monkeypatch,
        [target],
        [requested, available],
        [_row(target, available)],
    )

    async def load(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            None,
            "t-spawn",
            "target",
            ["vip"],
            datetime.now(UTC) + timedelta(minutes=5),
        )

    result = await _run_query(
        _request(),
        SelectionRequest(
            kind="selection",
            conversation_id=uuid.uuid4(),
            selection={"field": "target", "value": "vip"},
        ),
        load_conversation=load,
    )
    assert result["type"] == "not_found"
    assert result["reason"] == "NO_LOCAL_RESULT"


@pytest.mark.asyncio
async def test_selection_rechecks_active_fields_and_does_not_parse_slug_as_scope(
    monkeypatch,
) -> None:
    target = _catalog("flash", "窗口")
    origin = _catalog("a-ramp", "A1")
    _patch_query_dependencies(monkeypatch, [target], [origin], [_row(target, origin)])

    async def load(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            None,
            "archived-origin",
            "target",
            ["flash"],
            datetime.now(UTC) + timedelta(minutes=5),
        )

    response = await _run_query(
        _request(),
        SelectionRequest(
            kind="selection",
            conversation_id=uuid.uuid4(),
            selection={"field": "target", "value": "flash"},
        ),
        load_conversation=load,
    )
    assert response["type"] == "route_matches"
    assert response["intent"]["origin_slug"] is None
    assert [notice["code"] for notice in response["notices"]] == ["ORIGIN_UNRESOLVED"]


@pytest.mark.asyncio
async def test_target_only_returns_all_origins_without_opening_detail(monkeypatch) -> None:
    target = _catalog("vip", "VIP")
    t_spawn = _catalog("t-spawn", "T 出生点", sort_order=10)
    a_ramp = _catalog("a-ramp", "A1", sort_order=20)
    rows = [_row(target, t_spawn), _row(target, a_ramp)]
    _patch_query_dependencies(monkeypatch, [target], [t_spawn, a_ramp], rows)
    response = await _run_query(
        _request(), SimpleNamespace(conversation_id=None, text="我想封 VIP", input_mode="text")
    )
    assert response["type"] == "route_matches"
    assert response["intent"]["origin_slug"] is None
    assert [item["origin"]["slug"] for item in response["routes"]] == ["t-spawn", "a-ramp"]


@pytest.mark.asyncio
async def test_explicit_origin_ignores_action_words_and_matches_target_only_defaults(
    monkeypatch,
) -> None:
    target = _catalog("vip", "VIP")
    first_origin = _catalog("t-spawn", "T 出生点", sort_order=20, aliases=["匪家"])
    second_origin = _catalog("a-ramp", "A1", sort_order=10)
    rows = []
    for origin in (first_origin, second_origin):
        rows.extend(
            [
                _row(target, origin, default=False, sort_order=1, throw_text="无动作基线"),
                _row(target, origin, default=True, sort_order=99, throw_text="作者普通投掷说明"),
            ]
        )
    _patch_query_dependencies(monkeypatch, [target], [first_origin, second_origin], rows)

    target_only = await _run_query(
        _request(), SimpleNamespace(conversation_id=None, text="我想封 VIP", input_mode="text")
    )
    assert target_only["type"] == "route_matches"
    assert target_only["notices"] == []
    expected_by_origin = {
        row.origin.slug: {
            "id": str(row.lineup.id),
            "hint": public.lineup_summary(row)["variant_hint"],
        }
        for row in rows
        if row.lineup.is_default
    }
    assert [item["origin"]["slug"] for item in target_only["routes"]] == [
        "a-ramp",
        "t-spawn",
    ]
    assert [item["lineup"]["id"] for item in target_only["routes"]] == [
        expected_by_origin["a-ramp"]["id"],
        expected_by_origin["t-spawn"]["id"],
    ]
    assert [item["lineup"]["variant_hint"] for item in target_only["routes"]] == [
        expected_by_origin["a-ramp"]["hint"],
        expected_by_origin["t-spawn"]["hint"],
    ]

    action_texts = (
        "",
        "跳投",
        "蹲着跳投",
        "W+跳投",
        "A+跳投",
        "D+跳投",
        "任意非地点文字",
    )
    for action_text in action_texts:
        suffix = f" {action_text}" if action_text else ""
        target_only_with_action = await _run_query(
            _request(),
            SimpleNamespace(
                conversation_id=None,
                text=f"VIP{suffix}",
                input_mode="text",
            ),
        )
        assert target_only_with_action["type"] == "route_matches"
        assert target_only_with_action["routes"] == target_only["routes"]
        assert target_only_with_action["notices"] == target_only["notices"]
        assert target_only_with_action["intent"] == target_only["intent"]
        for origin in (first_origin, second_origin):
            response = await _run_query(
                _request(),
                SimpleNamespace(
                    conversation_id=None,
                    text=f"从{origin.name_zh}封 VIP{suffix}",
                    input_mode="text",
                ),
            )
            assert response["type"] == "match"
            assert response["notices"] == []
            assert response["intent"]["origin_slug"] == origin.slug
            assert response["lineup"]["id"] == expected_by_origin[origin.slug]["id"]
            assert (
                response["lineup"]["other_lineups"][0]["variant_hint"]
                == public.lineup_summary(
                    next(
                        row
                        for row in rows
                        if row.origin.slug == origin.slug and not row.lineup.is_default
                    )
                )["variant_hint"]
            )


@pytest.mark.asyncio
async def test_unknown_or_ambiguous_origin_is_cleared_and_explicit_missing_origin_fails(
    monkeypatch,
) -> None:
    target = _catalog("vip", "VIP")
    first = _catalog("t-spawn", "T 出生点")
    second = _catalog("a-ramp", "A1")
    rows = [_row(target, first), _row(target, second)]
    _patch_query_dependencies(monkeypatch, [target], [first, second], rows)

    class NoOriginIntent:
        async def parse(self, *_args, **_kwargs):
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[{"slug": "vip", "confidence": 0.92}],
                origin_candidates=[],
                target_mentioned=True,
                origin_mentioned=True,
                is_follow_up=False,
                explicitly_replaced_fields=["origin"],
            )

    unresolved = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="从未知位置封 VIP", input_mode="text"),
        providers=ProviderSet(NoOriginIntent()),
    )
    assert unresolved["type"] == "route_matches"
    assert [notice["code"] for notice in unresolved["notices"]] == ["ORIGIN_UNRESOLVED"]

    class GuessedOriginIntent:
        async def parse(self, *_args, **_kwargs):
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[{"slug": "vip", "confidence": 0.99}],
                origin_candidates=[{"slug": "t-spawn", "confidence": 0.99}],
                target_mentioned=True,
                origin_mentioned=True,
                is_follow_up=False,
                explicitly_replaced_fields=["origin"],
            )

    guessed = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="我在水果摊，想封 VIP 烟", input_mode="text"),
        providers=ProviderSet(GuessedOriginIntent()),
    )
    assert guessed["type"] == "route_matches"
    assert {item["origin"]["slug"] for item in guessed["routes"]} == {"t-spawn", "a-ramp"}
    assert [notice["code"] for notice in guessed["notices"]] == ["ORIGIN_UNRESOLVED"]

    ambiguous = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="T 出生点 A1 封 VIP", input_mode="text"),
    )
    assert ambiguous["type"] == "route_matches"
    assert ambiguous["intent"]["origin_slug"] is None

    missing = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="从 T 出生点封 VIP", input_mode="text"),
    )
    assert missing["type"] == "match"

    _patch_query_dependencies(monkeypatch, [target], [first], [_row(target, second)])
    result = await _run_query(
        _request(),
        SimpleNamespace(conversation_id=None, text="从 T 出生点封 VIP", input_mode="text"),
    )
    assert result["type"] == "not_found"
    assert result["reason"] == "NO_LOCAL_RESULT"


@pytest.mark.asyncio
async def test_search_selection_validates_pending_state_and_forces_slug(monkeypatch) -> None:
    conversation_id = uuid.uuid4()
    target = _catalog("vip", "VIP")
    origin = _catalog("t-spawn", "T 出生点")
    _patch_query_dependencies(monkeypatch, [target], [origin], [_row(target, origin)])

    async def load(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            None,
            "t-spawn",
            "target",
            ["vip"],
            datetime.now(UTC) + timedelta(minutes=5),
        )

    selection = SelectionRequest(
        kind="selection",
        conversation_id=conversation_id,
        selection={"field": "target", "value": "vip"},
    )
    response = await _run_query(_request(), selection, load_conversation=load)
    assert response["type"] == "match"

    async def expired(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            None,
            "t-spawn",
            "target",
            ["vip"],
            datetime.now(UTC) - timedelta(seconds=1),
        )

    with pytest.raises(AppError) as error:
        await _run_query(_request(), selection, load_conversation=expired)
    assert error.value.code == "INVALID_OR_EXPIRED_CLARIFICATION"

    with pytest.raises(ValidationError):
        SelectionRequest(
            kind="selection",
            conversation_id=conversation_id,
            selection={"field": "origin", "value": "t-spawn"},
        )


@pytest.mark.asyncio
async def test_public_map_target_and_detail_routes_share_rows(monkeypatch) -> None:
    target = _catalog("vip", "VIP", sort_order=10)
    origin = _catalog("t-spawn", "T 出生点", sort_order=20)
    row = _row(target, origin)
    _patch_query_dependencies(monkeypatch, [target], [origin], [row])
    map_request = _request("GET", "/api/v1/maps/mirage")
    map_response = await public.map_summary(map_request)
    assert map_response["published_lineup_count"] == 1
    assert map_response["targets"][0]["origin_count"] == 1
    assert map_request.state.public_event["response_type"] is None

    target_request = _request("GET", "/api/v1/targets/vip/lineups")
    targets = await public.target_lineups("vip", target_request)
    assert targets["origins"][0]["lineup"]["is_default"] is True
    assert target_request.state.public_event["response_type"] is None

    detail_request = _request("GET", f"/api/v1/lineups/{row.lineup.id}")
    detail = await public.public_lineup_detail(row.lineup.id, detail_request)
    assert detail["id"] == str(row.lineup.id)
    assert detail_request.state.public_event["response_type"] is None

    with pytest.raises(AppError) as error:
        await public.target_lineups("missing", _request("GET"))
    assert error.value.code == "TARGET_NOT_FOUND"
    with pytest.raises(AppError) as error:
        await public.public_lineup_detail(uuid.uuid4(), _request("GET"))
    assert error.value.code == "LINEUP_NOT_FOUND"


@pytest.mark.asyncio
async def test_public_aggregates_preserve_null_coordinate_pairs(monkeypatch) -> None:
    target = _catalog("vip", "VIP")
    origin = _catalog("t-spawn", "T 出生点")
    row = _row(target, origin)
    row.origin.map_x = None
    row.origin.map_y = None
    placeholder_target = _catalog("a-site", "A 点", sort_order=1)
    placeholder_origin = _catalog("ct-spawn", "CT 出生点", sort_order=1)
    placeholder_row = _row(placeholder_target, placeholder_origin, sort_order=1)
    placeholder_row.target.map_x = None
    placeholder_row.target.map_y = None
    _patch_query_dependencies(
        monkeypatch,
        [target, placeholder_target],
        [origin, placeholder_origin],
        [row, placeholder_row],
    )
    monkeypatch.setattr(
        public,
        "get_map_catalog",
        lambda: SimpleNamespace(
            slug="mirage",
            name_zh="荒漠迷城",
            asset_url="/assets/maps/mirage.svg",
            asset_status="placeholder",
        ),
    )

    map_payload = await public.map_summary(_request("GET", "/api/v1/maps/mirage"))
    assert map_payload["map"]["asset_status"] == "placeholder"
    map_targets = {item["slug"]: item for item in map_payload["targets"]}
    assert map_targets["vip"]["map_x"] == 50.0
    assert map_targets["vip"]["map_y"] == 25.0
    assert map_targets["a-site"]["map_x"] is None
    assert map_targets["a-site"]["map_y"] is None
    target_payload = await public.target_lineups("vip", _request("GET"))
    assert target_payload["target"]["map_x"] == 50.0
    assert target_payload["origins"][0]["origin"]["map_y"] is None
    detail_payload = await public.public_lineup_detail(row.lineup.id, _request("GET"))
    assert detail_payload["target"]["map_x"] == 50.0
    assert detail_payload["origin"]["map_y"] is None


@pytest.mark.asyncio
async def test_approved_public_map_requires_every_published_coordinate(monkeypatch) -> None:
    target = _catalog("vip", "VIP")
    origin = _catalog("t-spawn", "T 出生点")
    row = _row(target, origin)
    row.target.map_x = 50
    row.target.map_y = 25
    row.origin.map_x = None
    row.origin.map_y = None
    _patch_query_dependencies(monkeypatch, [target], [origin], [row])
    monkeypatch.setattr(
        public,
        "get_map_catalog",
        lambda: SimpleNamespace(
            slug="mirage",
            name_zh="荒漠迷城",
            asset_url="/assets/maps/mirage.svg",
            asset_status="approved",
        ),
    )

    payload = await public.map_summary(_request("GET", "/api/v1/maps/mirage"))
    assert payload["map"]["asset_status"] == "approved"
    assert payload["targets"][0]["origin_count"] == 1

    row.target.map_x = None
    row.target.map_y = None
    request = _request("GET", "/api/v1/maps/mirage")
    with pytest.raises(AppError) as error:
        await public.map_summary(request)
    assert (error.value.status_code, error.value.code, error.value.details) == (
        503,
        "CONTENT_CONFIGURATION_ERROR",
        None,
    )
    assert error.value.message == "公开内容配置暂时异常，请稍后重试。"
    assert request.state.public_event["error_code"] == "CONTENT_CONFIGURATION_ERROR"


def test_public_business_events_are_recorded_once_on_success_404_and_configuration_error(
    monkeypatch,
) -> None:
    events: list[dict[str, object]] = []

    async def record(request_id: str, **values) -> None:
        events.append({"request_id": request_id, **values})

    @asynccontextmanager
    async def snapshot():
        yield object()

    async def no_rows(_session):
        return []

    monkeypatch.setattr("app.main.record_public_request", record)
    monkeypatch.setattr(public, "public_snapshot", snapshot)
    monkeypatch.setattr(public, "available_rows", no_rows)
    with TestClient(app, raise_server_exceptions=False) as client:
        success = client.get("/api/v1/maps/mirage")
        missing = client.get(f"/api/v1/lineups/{uuid.uuid4()}")

    assert success.status_code == 200
    assert missing.status_code == 404
    assert len(events) == 2
    assert events[0]["response_type"] is None
    assert events[0]["error_code"] is None
    assert events[1]["error_code"] == "LINEUP_NOT_FOUND"

    target = _catalog("vip", "VIP")
    origin = _catalog("t-spawn", "T 出生点")
    conflicting = _row(target, origin)
    conflicting.lineup.is_default = True

    async def conflicting_rows(_session):
        return [conflicting, _row(target, origin)]

    monkeypatch.setattr(public, "available_rows", conflicting_rows)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/targets/vip/lineups")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTENT_CONFIGURATION_ERROR"
    assert len(events) == 3
    assert events[-1]["error_code"] == "CONTENT_CONFIGURATION_ERROR"


@pytest.mark.asyncio
async def test_cross_field_same_span_requires_unavailable_intent_parser(monkeypatch) -> None:
    target = _catalog("vip", "VIP")
    origin = _catalog("window-position", "窗口位置", aliases=["VIP"])
    _patch_query_dependencies(monkeypatch, [target], [origin], [_row(target, origin)])
    with pytest.raises(AppError) as error:
        await _run_query(
            _request(), SimpleNamespace(conversation_id=None, text="VIP 烟", input_mode="text")
        )
    assert (error.value.status_code, error.value.code) == (503, "INTENT_AI_UNAVAILABLE")

    # A same-span Target/Origin ambiguity must still reach Intent.
    second = _catalog("another-vip", "另一个窗口", aliases=["VIP"])
    _patch_query_dependencies(monkeypatch, [target, second], [origin], [])
    with pytest.raises(AppError) as cross_field_error:
        await _run_query(
            _request(), SimpleNamespace(conversation_id=None, text="VIP 烟", input_mode="text")
        )
    assert cross_field_error.value.code == "INTENT_AI_UNAVAILABLE"


@pytest.mark.asyncio
async def test_long_origin_consumes_inner_short_target_across_fields(monkeypatch) -> None:
    inner_target = _catalog("vip", "VIP")
    target = _catalog("ct", "CT")
    origin = _catalog("small-vip", "小VIP")
    _patch_query_dependencies(monkeypatch, [inner_target, target], [origin], [_row(target, origin)])
    response = await _run_query(
        _request(), SimpleNamespace(conversation_id=None, text="从小VIP封 CT", input_mode="text")
    )
    assert response["type"] == "match"
    assert response["intent"]["target_slug"] == "ct"
    assert response["intent"]["origin_slug"] == "small-vip"


@pytest.mark.asyncio
async def test_selection_slug_is_not_reinterpreted_as_origin(monkeypatch) -> None:
    target = _catalog("vip", "窗口")
    origin = _catalog("window-position", "VIP")
    _patch_query_dependencies(monkeypatch, [target], [origin], [_row(target, origin)])

    async def load(_conversation_id, _delete_expired=True):
        return ConversationSnapshot(
            None,
            None,
            "target",
            ["vip"],
            datetime.now(UTC) + timedelta(minutes=5),
        )

    response = await _run_query(
        _request(),
        SelectionRequest(
            kind="selection",
            conversation_id=uuid.uuid4(),
            selection={"field": "target", "value": "vip"},
        ),
        load_conversation=load,
    )
    assert response["type"] == "route_matches"
    assert response["intent"]["origin_slug"] is None
    assert response["notices"] == []


def test_public_openapi_uses_shared_response_models() -> None:
    schema = app.openapi()
    for path, expected in (
        ("/api/v1/maps/mirage", "MapResponse"),
        ("/api/v1/targets/{target_slug}/lineups", "TargetChoices"),
        ("/api/v1/lineups/{lineup_id}", "LineupDetail"),
    ):
        result = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
        assert result["schema"] == {"$ref": f"#/components/schemas/{expected}"}
    search = schema["paths"]["/api/v1/search"]["post"]["responses"]["200"]
    union = search["content"]["application/json"]["schema"]
    assert union["discriminator"]["propertyName"] == "type"
    assert set(union["discriminator"]["mapping"]) == {
        "match",
        "route_matches",
        "clarification",
        "not_found",
        "out_of_scope",
    }


def test_validation_paths_and_target_slug_constraints(monkeypatch) -> None:
    async def no_event(*_args, **_kwargs):
        pass

    monkeypatch.setattr("app.main.record_public_request", no_event)
    with TestClient(app) as client:
        selection = client.post(
            "/api/v1/search",
            json={
                "kind": "selection",
                "conversation_id": str(uuid.uuid4()),
                "selection": {"field": "origin", "value": "vip"},
            },
        )
        invalid_slug = client.get("/api/v1/targets/BAD_SLUG/lineups")
    assert selection.status_code == 422
    assert selection.json()["error"]["details"]["fields"][0]["path"] == "selection.field"
    assert invalid_slug.status_code == 422
    assert invalid_slug.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/problem+json"])
def test_search_content_type_is_rejected_before_body_parsing(monkeypatch, content_type) -> None:
    async def no_event(*_args, **_kwargs):
        pass

    monkeypatch.setattr("app.main.record_public_request", no_event)
    headers = {"Content-Type": content_type} if content_type else {}
    with TestClient(app) as client:
        response = client.post("/api/v1/search", content="not-json", headers=headers)
        missing = client.post("/api/v1/not-an-endpoint", content="not-json", headers=headers)
        invalid_json = client.post(
            "/api/v1/search", content="not-json", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_CONTENT_TYPE"
    assert missing.status_code == 404
    assert invalid_json.status_code == 400
    assert invalid_json.json()["error"]["code"] == "INVALID_JSON"


@pytest.mark.asyncio
@pytest.mark.parametrize("state_kind", ["missing", "expired", "active"])
async def test_query_pending_does_not_revive_missing_or_expired_session(
    monkeypatch, state_kind: str
) -> None:
    previous_id = uuid.uuid4()
    now = datetime.now(UTC)
    state = (
        None
        if state_kind == "missing"
        else SimpleNamespace(
            expires_at=now + timedelta(minutes=5 if state_kind == "active" else -5),
            pending_field="target",
            pending_options=["vip"],
            target_slug="vip",
            origin_slug="t-spawn",
        )
    )

    @asynccontextmanager
    async def scope():
        yield session

    session = SimpleNamespace(begin=scope, get=AsyncMock(return_value=state), delete=AsyncMock())
    monkeypatch.setattr("app.services.agent_graph.get_session_factory", lambda: scope)
    result = await clear_pending_state(previous_id)
    if state_kind == "active":
        assert result.conversation_id == previous_id
        assert result.previous is not None
        assert state.pending_field is None and state.pending_options is None
        assert state.target_slug == "vip" and state.origin_slug == "t-spawn"
        assert state.expires_at > now + timedelta(minutes=29)
        session.delete.assert_not_awaited()
    else:
        assert result.conversation_id != previous_id
        assert result.previous is None
        if state_kind == "expired":
            session.delete.assert_awaited_once_with(state)
        else:
            session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_public_visibility_excludes_whitespace_only_steps() -> None:
    target, origin = (
        _catalog("vip", "VIP"),
        _catalog("t-spawn", "T"),
    )
    valid = _row(target, origin)
    hidden = _row(target, origin)
    hidden.lineup.aim_text = "\n\r\u3000\u0085"
    rows = [(row.lineup, row.target, row.origin, row.video) for row in (valid, hidden)]
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    result = await available_rows(session)
    assert [row.lineup.id for row in result] == [valid.lineup.id]


def test_transcription_multipart_contract_and_public_event(monkeypatch) -> None:
    from app.services.providers import Transcription

    events: list[dict[str, object]] = []

    async def record(_request_id: str, **kwargs: object) -> None:
        events.append(kwargs)

    async def fake_transcribe(upload, **_kwargs):
        assert upload.filename == "recording.webm"
        return Transcription("我在 Mirage 的 T 出生点，想封 VIP 烟", 1.0), 1.25

    monkeypatch.setattr("app.main.record_public_request", record)
    monkeypatch.setattr(public, "transcribe_upload", fake_transcribe)
    with TestClient(app, raise_server_exceptions=False) as client:
        valid = client.post(
            "/api/v1/transcriptions",
            files={"audio": ("recording.webm", b"audio", "audio/webm")},
        )
        missing = client.post("/api/v1/transcriptions", files=[("other", (None, "value"))])
        repeated = client.post(
            "/api/v1/transcriptions",
            files=[
                ("audio", ("one.webm", b"one", "audio/webm")),
                ("audio", ("two.webm", b"two", "audio/webm")),
            ],
        )
        extra = client.post(
            "/api/v1/transcriptions",
            files=[
                ("audio", ("recording.webm", b"audio", "audio/webm")),
                ("extra", (None, "value")),
            ],
        )
        wrong_type = client.post(
            "/api/v1/transcriptions",
            content=b"not multipart",
            headers={"Content-Type": "application/json"},
        )
    assert valid.status_code == 200
    assert valid.json() == {
        "text": "我在 Mirage 的 T 出生点，想封 VIP 烟",
        "duration_seconds": 1.25,
    }
    assert [response.json()["error"]["code"] for response in (missing, repeated, extra)] == [
        "VALIDATION_ERROR",
        "VALIDATION_ERROR",
        "VALIDATION_ERROR",
    ]
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "UNSUPPORTED_CONTENT_TYPE"
    assert len(events) == 5
    assert events[0]["input_mode"] == "voice"
    assert events[0]["response_type"] is None
