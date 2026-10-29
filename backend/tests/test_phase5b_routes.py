"""Focused Phase 5B route-matches coverage without a database or provider call."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import LineupStatus, VideoStatus
from app.schemas.agent import IntentExtraction
from app.services.agent_graph import (
    ConversationSnapshot,
    PendingClearResult,
    run_search,
)
from app.services.local_search import CatalogEntity, PublicRow
from app.services.normalization import catalog_terms
from app.services.providers import ProviderSet


def _settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        postgres_password="smokeline",
        ai_provider="disabled",
    )


def _entity(
    slug: str, name: str, *, sort_order: int, aliases: list[str] | None = None
) -> CatalogEntity:
    return CatalogEntity(
        uuid.uuid4(),
        slug,
        name,
        None,
        catalog_terms(name, None, aliases or []),
        sort_order,
    )


def _row(
    target: CatalogEntity, origin: CatalogEntity, *, default: bool, sort_order: int
) -> PublicRow:
    target_row = SimpleNamespace(
        id=target.id,
        slug=target.slug,
        name_zh=target.name_zh,
        map_x=None,
        map_y=None,
        sort_order=target.sort_order,
        is_active=True,
    )
    origin_row = SimpleNamespace(
        id=origin.id,
        slug=origin.slug,
        name_zh=origin.name_zh,
        map_x=None,
        map_y=None,
        sort_order=origin.sort_order,
        is_active=True,
    )
    lineup = SimpleNamespace(
        id=uuid.uuid4(),
        target_id=target.id,
        origin_id=origin.id,
        status=LineupStatus.published,
        is_default=default,
        sort_order=sort_order,
        setup_text="站到箱子旁",
        aim_text="瞄准墙角",
        throw_text="投掷说明",
        result_text="封住目标",
        last_checked_at=None,
        game_version_note=None,
    )
    video = SimpleNamespace(
        id=uuid.uuid4(),
        status=VideoStatus.ready,
        storage_key=uuid.uuid4(),
        relative_path=f"{lineup.id}/primary.mp4",
        poster_relative_path=f"{lineup.id}/poster.webp",
        duration_ms=1_000,
        checksum_sha256="a" * 64,
    )
    return PublicRow(lineup, target_row, origin_row, video)


def _context(graph, targets, origins, rows):
    @asynccontextmanager
    async def snapshot():
        yield object()

    async def load_catalog(_session):
        return targets, origins

    async def load_rows(_session, **_kwargs):
        return rows

    request = SimpleNamespace(state=SimpleNamespace(request_id=uuid.uuid4().hex))
    body = SimpleNamespace(kind="query", input_mode="text", conversation_id=None)
    return graph.SearchContext(
        request,
        body,
        _settings(),
        catalog_loader=load_catalog,
        rows_loader=load_rows,
        snapshot_factory=snapshot,
        deadline=time.monotonic() + 10,
    )


async def _resolve_and_retrieve(graph, ctx, text: str):
    state = {
        "request_kind": "query",
        "raw_text": text,
        "normalized_text": graph.normalize_text(text),
        "previous_intent": None,
    }
    state.update(await graph._deterministic_parse(state, ctx))
    assert state["deterministic_result"]["route"] == "resolve"
    state.update(await graph._resolve_intent(state, ctx))
    if state.get("response_type") is not None:
        return state
    state.update(await graph._retrieve_local(state, ctx))
    return state


@pytest.mark.asyncio
async def test_phase5b_four_query_modes_are_local_and_deterministic() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target_a = _entity("vip", "警家", sort_order=20)
    target_b = _entity("ct", "CT", sort_order=10)
    origin_b = _entity("t-spawn", "匪家", sort_order=20)
    origin_a = _entity("a-ramp", "A1", sort_order=10)
    rows = [
        _row(target_a, origin_b, default=False, sort_order=1),
        _row(target_a, origin_b, default=True, sort_order=9),
        _row(target_b, origin_a, default=True, sort_order=2),
    ]
    ctx = _context(graph, [target_a, target_b], [origin_b, origin_a], rows)

    exact = await _resolve_and_retrieve(graph, ctx, "匪家 警家")
    assert exact["response_type"] == "match"
    assert exact["query_mode"] == "exact"
    assert exact["target_mentioned"] is True
    assert exact["origin_mentioned"] is True

    target_only = await _resolve_and_retrieve(graph, ctx, "警家烟怎么丢")
    assert target_only["response_type"] == "route_matches"
    assert target_only["query_mode"] == "target_only"
    assert target_only["target_mentioned"] is True
    assert target_only["origin_mentioned"] is False
    assert len(target_only["local_lineups"]) == 1
    assert target_only["local_lineups"][0]["lineup"]["is_default"] is True

    origin_only = await _resolve_and_retrieve(graph, ctx, "从匪家怎么丢烟")
    assert origin_only["response_type"] == "route_matches"
    assert origin_only["query_mode"] == "origin_only"
    assert origin_only["target_mentioned"] is False
    assert origin_only["origin_mentioned"] is True

    catalog = await _resolve_and_retrieve(graph, ctx, "烟怎么丢")
    assert catalog["response_type"] == "route_matches"
    assert catalog["query_mode"] == "catalog"
    assert catalog["target_mentioned"] is False
    assert catalog["origin_mentioned"] is False
    assert [item["target"]["slug"] for item in catalog["local_lineups"]] == ["ct", "vip"]


@pytest.mark.asyncio
async def test_run_search_route_modes_are_end_to_end_and_never_call_intent_provider() -> None:
    target_vip = _entity("vip", "警家", sort_order=20)
    target_ct = _entity("ct", "CT", sort_order=10)
    origin_t = _entity("t-spawn", "匪家", sort_order=20)
    origin_a = _entity("a-ramp", "A1", sort_order=10)
    rows = [
        _row(target_vip, origin_t, default=False, sort_order=1),
        _row(target_vip, origin_t, default=True, sort_order=9),
        _row(target_ct, origin_t, default=True, sort_order=3),
        _row(target_ct, origin_a, default=True, sort_order=2),
    ]

    @asynccontextmanager
    async def snapshot():
        yield object()

    async def load_catalog(_session):
        return [target_vip, target_ct], [origin_t, origin_a]

    async def load_rows(_session, **_kwargs):
        return rows

    async def no_save(*_args, **_kwargs):
        return None

    class FailingIntent:
        def __init__(self) -> None:
            self.calls = 0

        async def parse(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("Phase 5B deterministic teaching grammar called the LLM")

    provider = FailingIntent()

    async def search(text: str, conversation_id=None, active_provider=provider):
        request = SimpleNamespace(state=SimpleNamespace(request_id=uuid.uuid4().hex))
        body = SimpleNamespace(
            kind="query",
            text=text,
            input_mode="text",
            conversation_id=conversation_id,
        )
        return await run_search(
            request,
            body,
            settings=_settings(),
            providers=ProviderSet(active_provider),
            clear_pending=lambda conversation: _pending_clear(conversation),
            save_conversation=no_save,
            catalog_loader=load_catalog,
            rows_loader=load_rows,
            snapshot_factory=snapshot,
        )

    target_only = await search("警家烟怎么丢")
    assert target_only["type"] == "route_matches"
    assert target_only["intent"]["target_slug"] == "vip"
    assert target_only["intent"]["origin_slug"] is None
    assert len(target_only["routes"]) == 1

    origin_only = await search("从匪家怎么丢烟")
    assert origin_only["type"] == "route_matches"
    assert origin_only["intent"]["target_slug"] is None
    assert origin_only["intent"]["origin_slug"] == "t-spawn"
    assert [route["target"]["slug"] for route in origin_only["routes"]] == ["ct", "vip"]

    catalog = await search("烟怎么丢")
    assert catalog["type"] == "route_matches"
    assert catalog["intent"] == {
        "map": "mirage",
        "grenade": "smoke",
        "target_slug": None,
        "origin_slug": None,
    }
    assert [route["target"]["slug"] for route in catalog["routes"]] == ["ct", "ct", "vip"]

    class UnknownOriginIntent:
        async def parse(self, *_args, **_kwargs):
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[{"slug": "vip", "confidence": 0.99}],
                origin_candidates=[],
                target_mentioned=True,
                origin_mentioned=True,
                is_follow_up=False,
                explicitly_replaced_fields=["origin"],
            )

    unknown_origin = await search("从未知起点封警家烟", active_provider=UnknownOriginIntent())
    assert unknown_origin["type"] == "route_matches"
    assert [notice["code"] for notice in unknown_origin["notices"]] == ["ORIGIN_UNRESOLVED"]
    assert {route["origin"]["slug"] for route in unknown_origin["routes"]} == {"t-spawn"}

    previous_id = uuid.uuid4()
    inherited = await search("烟怎么丢", previous_id)
    assert inherited["type"] == "route_matches"
    assert inherited["intent"]["target_slug"] is None
    assert inherited["intent"]["origin_slug"] is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_phase5b_colloquial_origin_catalog_phrases_are_local() -> None:
    target_ct = _entity("ct", "警家", sort_order=10)
    target_vip = _entity("vip", "VIP", sort_order=20)
    origin_t = _entity("t-spawn", "匪家", sort_order=10)
    origin_sandbags = _entity("sandbags", "沙袋", sort_order=20)
    rows = [
        _row(target_ct, origin_t, default=True, sort_order=1),
        _row(target_vip, origin_t, default=True, sort_order=2),
        _row(target_ct, origin_sandbags, default=True, sort_order=3),
    ]

    @asynccontextmanager
    async def snapshot():
        yield object()

    async def load_catalog(_session):
        return [target_ct, target_vip], [origin_t, origin_sandbags]

    async def load_rows(_session, **_kwargs):
        return rows

    async def no_save(*_args, **_kwargs):
        return None

    class FailingIntent:
        def __init__(self) -> None:
            self.calls = 0

        async def parse(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("colloquial origin catalog requests must stay deterministic")

    provider = FailingIntent()

    async def search(text: str):
        request = SimpleNamespace(state=SimpleNamespace(request_id=uuid.uuid4().hex))
        body = SimpleNamespace(
            kind="query",
            text=text,
            input_mode="text",
            conversation_id=None,
        )
        return await run_search(
            request,
            body,
            settings=_settings(),
            providers=ProviderSet(provider),
            clear_pending=lambda conversation: _pending_clear(conversation),
            save_conversation=no_save,
            catalog_loader=load_catalog,
            rows_loader=load_rows,
            snapshot_factory=snapshot,
        )

    t_spawn = await search("匪家起手烟都有哪些")
    assert t_spawn["type"] == "route_matches"
    assert t_spawn["intent"]["origin_slug"] == "t-spawn"

    sandbags = await search("沙袋这儿能扔哪些烟")
    assert sandbags["type"] == "route_matches"
    assert sandbags["intent"]["origin_slug"] == "sandbags"
    assert provider.calls == 0


async def _pending_clear(conversation_id: uuid.UUID) -> PendingClearResult:
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


@pytest.mark.asyncio
async def test_phase5b_default_group_configuration_is_a_503_and_nondefault_is_not_listed() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target = _entity("vip", "警家", sort_order=1)
    origin = _entity("t-spawn", "匪家", sort_order=1)

    for rows in (
        [_row(target, origin, default=False, sort_order=1)],
        [
            _row(target, origin, default=True, sort_order=1),
            _row(target, origin, default=True, sort_order=2),
        ],
    ):
        ctx = _context(graph, [target], [origin], rows)
        with pytest.raises(AppError) as raised:
            await _resolve_and_retrieve(graph, ctx, "警家烟怎么丢")
        assert raised.value.status_code == 503
        assert raised.value.code == "CONTENT_CONFIGURATION_ERROR"

    empty_ctx = _context(graph, [target], [origin], [])
    empty = await _resolve_and_retrieve(graph, empty_ctx, "警家烟怎么丢")
    assert empty["response_type"] == "not_found"
    assert empty["error_code"] == "NO_LOCAL_RESULT"


@pytest.mark.asyncio
async def test_phase5b_stable_sorting_deduplicates_same_group_and_resolves_unknowns() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target_vip = _entity("vip", "警家", sort_order=20)
    target_ct = _entity("ct", "CT", sort_order=10)
    origin_t = _entity("t-spawn", "匪家", sort_order=20)
    origin_a = _entity("a-ramp", "A1", sort_order=10)
    rows = [
        _row(target_vip, origin_t, default=False, sort_order=1),
        _row(target_vip, origin_t, default=True, sort_order=9),
        _row(target_ct, origin_a, default=True, sort_order=2),
    ]
    ctx = _context(graph, [target_vip, target_ct], [origin_t, origin_a], rows)

    target_origin = await _resolve_and_retrieve(graph, ctx, "警家 匪家")
    assert target_origin["response_type"] == "match"
    assert len(target_origin["local_lineups"]) == 1
    assert target_origin["local_lineups"][0]["other_lineups"]

    unknown_target = await _resolve_and_retrieve(graph, ctx, "封不存在的点")
    assert unknown_target["response_type"] == "not_found"
    assert unknown_target["error_code"] == "NO_TARGET"


@pytest.mark.asyncio
async def test_phase5b_unknown_target_is_not_consumed_by_colloquial_catalog_skeleton() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target = _entity("vip", "警家", sort_order=1)
    origin = _entity("t-spawn", "匪家", sort_order=1)
    ctx = _context(graph, [target], [origin], [_row(target, origin, default=True, sort_order=1)])
    state = {
        "request_kind": "query",
        "raw_text": "匪家不存在的点能扔哪些烟",
        "normalized_text": graph.normalize_text("匪家不存在的点能扔哪些烟"),
        "previous_intent": None,
    }

    state.update(await graph._deterministic_parse(state, ctx))
    deterministic = state["deterministic_result"]
    assert deterministic["route"] == "resolve"
    assert deterministic["teaching_skeleton"] is False
    assert deterministic["explicit_unknown_target"] is True
    assert deterministic["origin_matches"] == ["t-spawn"]
    resolved = await graph._resolve_intent(state, ctx)
    assert resolved["response_type"] == "not_found"
    assert resolved["error_code"] == "NO_TARGET"


@pytest.mark.asyncio
async def test_phase5b_unknowns_do_not_broaden_or_adopt_guesses() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target = _entity("vip", "警家", sort_order=1)
    origin = _entity("t-spawn", "匪家", sort_order=1)
    rows = [_row(target, origin, default=True, sort_order=1)]
    ctx = _context(graph, [target], [origin], rows)

    unknown_target = {
        "request_kind": "query",
        "raw_text": "未收录目标怎么丢烟",
        "normalized_text": graph.normalize_text("未收录目标怎么丢烟"),
        "previous_intent": None,
    }
    unknown_target.update(await graph._deterministic_parse(unknown_target, ctx))
    unknown_target["llm_extraction"] = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[],
        origin_candidates=[],
        target_mentioned=True,
        origin_mentioned=False,
        is_follow_up=False,
        explicitly_replaced_fields=[],
    )
    resolved = await graph._resolve_intent(unknown_target, ctx)
    assert resolved["response_type"] == "not_found"
    assert resolved["error_code"] == "NO_TARGET"
    assert resolved["resolved_intent"] == {
        "map": "mirage",
        "grenade": "smoke",
        "target_slug": None,
        "origin_slug": None,
    }

    unknown_target_with_origin = {
        "request_kind": "query",
        "raw_text": "匪家封未收录目标",
        "normalized_text": graph.normalize_text("匪家封未收录目标"),
        "previous_intent": None,
    }
    unknown_target_with_origin.update(
        await graph._deterministic_parse(unknown_target_with_origin, ctx)
    )
    unknown_target_with_origin["llm_extraction"] = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[],
        origin_candidates=[],
        target_mentioned=True,
        origin_mentioned=True,
        is_follow_up=False,
        explicitly_replaced_fields=["target"],
    )
    validated = await graph._validate_llm_output(unknown_target_with_origin, ctx)
    unknown_target_with_origin.update(validated)
    resolved_with_origin = await graph._resolve_intent(unknown_target_with_origin, ctx)
    assert resolved_with_origin["response_type"] == "not_found"
    assert resolved_with_origin["error_code"] == "NO_TARGET"
    assert resolved_with_origin["resolved_intent"]["target_slug"] is None
    assert resolved_with_origin["resolved_intent"]["origin_slug"] == "t-spawn"

    unknown_origin = {
        "request_kind": "query",
        "raw_text": "从未知起点封警家烟",
        "normalized_text": graph.normalize_text("从未知起点封警家烟"),
        "previous_intent": None,
    }
    unknown_origin.update(await graph._deterministic_parse(unknown_origin, ctx))
    unknown_origin["llm_extraction"] = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[],
        origin_candidates=[],
        target_mentioned=True,
        origin_mentioned=True,
        is_follow_up=False,
        explicitly_replaced_fields=["origin"],
    )
    resolved = await graph._resolve_intent(unknown_origin, ctx)
    assert resolved["resolved_intent"]["target_slug"] == "vip"
    assert [notice["code"] for notice in resolved["notices"]] == ["ORIGIN_UNRESOLVED"]
    unknown_origin.update(resolved)
    retrieved = await graph._retrieve_local(unknown_origin, ctx)
    assert retrieved["response_type"] == "route_matches"
    assert len(retrieved["local_lineups"]) == 1
