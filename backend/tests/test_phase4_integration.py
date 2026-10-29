"""Opt-in PostgreSQL evidence for Phase 4 quota and conversation contracts."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

pytest.importorskip("langgraph")

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import ConversationState, ExternalSearchCache, Origin, Target, UsageEvent
from app.db.session import get_session_factory
from app.services.agent_graph import (
    ConversationSnapshot,
    clear_pending_state,
    conversation_lock,
    load_conversation_state,
    run_search,
    save_conversation_state,
)
from app.services.local_search import active_catalog, public_snapshot
from app.services.providers import (
    DisabledIntentParser,
    ProviderFailure,
    ProviderSet,
    finish_provider_call,
    reserve_provider_call,
    shanghai_day_bounds,
)

pytestmark = pytest.mark.integration


def _require_isolated_stack() -> None:
    if os.environ.get("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("Phase 4 PostgreSQL evidence requires the isolated verification stack")


def _test_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        "postgres_password": "smokeline",
        "ai_provider": "fake",
    }
    values.update(overrides)
    return Settings(**values)


async def _delete_rows(*, usage_ids: list[UUID], conversation_ids: list[UUID]) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            if usage_ids:
                await session.execute(sa.delete(UsageEvent).where(UsageEvent.id.in_(usage_ids)))
            if conversation_ids:
                await session.execute(
                    sa.delete(ConversationState).where(ConversationState.id.in_(conversation_ids))
                )


@pytest.mark.asyncio
async def test_postgres_legacy_search_cache_table_remains_untouched_by_runtime() -> None:
    _require_isolated_stack()
    import app.services.agent_graph as graph

    assert not hasattr(graph, "_read_cache")
    assert not hasattr(graph, "_write_cache")
    suffix = uuid4().hex[:10]
    cache_key = f"v3|phase4-v16-{suffix}|legacy"
    target = Target(
        id=uuid4(),
        slug=f"phase4-cache-target-{suffix}",
        name_zh=f"缓存目标{suffix}",
        name_en=None,
        description="legacy cache preservation fixture",
        map_x=50,
        map_y=50,
        sort_order=9500,
        is_active=True,
    )
    cache_row = ExternalSearchCache(
        cache_key=cache_key,
        payload=[{"title": "legacy marker", "url": "https://www.bilibili.com/video/legacy"}],
        is_empty=False,
        expires_at=datetime(2030, 1, 2, tzinfo=UTC),
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    conversation_id: UUID | None = None
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                table_name = (
                    await session.execute(
                        sa.text("SELECT to_regclass('public.external_search_cache')")
                    )
                ).scalar_one()
                assert table_name == "external_search_cache"
                session.add_all([target, cache_row])

        before = (
            cache_row.cache_key,
            cache_row.payload,
            cache_row.is_empty,
            cache_row.expires_at,
            cache_row.created_at,
        )
        result = await run_search(
            _request(),
            SimpleNamespace(
                kind="query",
                text=target.name_zh,
                conversation_id=None,
                input_mode="text",
            ),
            settings=_test_settings(ai_provider="disabled"),
            providers=ProviderSet(DisabledIntentParser()),
        )
        assert result["type"] == "not_found"
        assert result["reason"] == "NO_LOCAL_RESULT"
        conversation_id = UUID(str(result["conversation_id"]))

        async with get_session_factory()() as session:
            preserved = await session.get(ExternalSearchCache, cache_key)
            assert preserved is not None
            after = (
                preserved.cache_key,
                preserved.payload,
                preserved.is_empty,
                preserved.expires_at,
                preserved.created_at,
            )
            assert after == before
    finally:
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    sa.delete(ExternalSearchCache).where(ExternalSearchCache.cache_key == cache_key)
                )
                await session.execute(sa.delete(Target).where(Target.id == target.id))
                if conversation_id is not None:
                    await session.execute(
                        sa.delete(ConversationState).where(ConversationState.id == conversation_id)
                    )


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(request_id=uuid4().hex))


async def _insert_active_points() -> tuple[Target, Origin]:
    suffix = uuid4().hex[:10]
    target = Target(
        id=uuid4(),
        slug=f"phase4-target-{suffix}",
        name_zh="Phase 4 目标",
        name_en="Phase 4 Target",
        description="integration fixture",
        map_x=40,
        map_y=40,
        sort_order=9000,
        is_active=True,
    )
    origin = Origin(
        id=uuid4(),
        slug=f"phase4-origin-{suffix}",
        name_zh="Phase 4 起点",
        name_en="Phase 4 Origin",
        description="integration fixture",
        map_x=20,
        map_y=20,
        sort_order=9000,
        is_active=True,
    )
    async with get_session_factory()() as session:
        async with session.begin():
            session.add_all([target, origin])
    return target, origin


async def _delete_points(target_id: UUID, origin_id: UUID) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(sa.delete(Target).where(Target.id == target_id))
            await session.execute(sa.delete(Origin).where(Origin.id == origin_id))


def _local_row(target: Target, origin: Origin) -> SimpleNamespace:
    lineup = SimpleNamespace(
        id=uuid4(),
        target_id=target.id,
        origin_id=origin.id,
        status="published",
        is_default=True,
        sort_order=0,
        setup_text="站位",
        aim_text="瞄点",
        throw_text="普通投掷",
        result_text="封烟",
        last_checked_at=None,
        game_version_note=None,
    )
    return SimpleNamespace(
        lineup=lineup,
        target=SimpleNamespace(
            id=target.id,
            slug=target.slug,
            name_zh=target.name_zh,
            map_x=target.map_x,
            map_y=target.map_y,
            sort_order=target.sort_order,
            is_active=True,
        ),
        origin=SimpleNamespace(
            id=origin.id,
            slug=origin.slug,
            name_zh=origin.name_zh,
            map_x=origin.map_x,
            map_y=origin.map_y,
            sort_order=origin.sort_order,
            is_active=True,
        ),
        video=SimpleNamespace(
            relative_path="phase4/route.mp4",
            poster_relative_path="phase4/route.webp",
            checksum_sha256="a" * 64,
            duration_ms=1000,
        ),
    )


@pytest.mark.asyncio
async def test_postgres_quota_concurrency_day_boundary_and_outcomes() -> None:
    _require_isolated_stack()
    settings = _test_settings(daily_llm_call_limit=2)
    request_ids = [uuid4() for _ in range(3)]
    now = datetime.now(UTC)
    start, end = shanghai_day_bounds(now)
    reservations = []
    boundary_reservations = []
    old_id = uuid4()
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                session.add(
                    UsageEvent(
                        id=old_id,
                        request_id=uuid4(),
                        event_type="provider_reservation",
                        input_mode="text",
                        provider_type="intent",
                        reserved_units=1,
                        provider_outcome="success",
                        created_at=start - timedelta(seconds=1),
                    )
                )

        async def reserve(request_id: UUID):
            return await reserve_provider_call(
                request_id,
                "text",
                "intent",
                settings=settings,
                now=now,
            )

        results = await asyncio.gather(
            *(reserve(request_id) for request_id in request_ids), return_exceptions=True
        )
        successful = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, AppError)]
        assert len(successful) == 2
        assert len(failures) == 1
        assert failures[0].code == "DAILY_LLM_QUOTA_EXHAUSTED"
        reservations.extend(successful)

        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(UsageEvent).where(
                            UsageEvent.id.in_([item.id for item in reservations])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
            assert all(start <= row.created_at < end for row in rows)
            assert all(row.provider_outcome == "reserved" for row in rows)

        await finish_provider_call(
            reservations[0],
            "success",
            input_tokens=12,
            output_tokens=8,
        )
        await finish_provider_call(
            reservations[1],
            "failed",
            error_code="INTENT_AI_UPSTREAM_ERROR",
        )
        with pytest.raises(AppError) as exhausted_after_outcomes:
            await reserve(uuid4())
        assert exhausted_after_outcomes.value.code == "DAILY_LLM_QUOTA_EXHAUSTED"
        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(UsageEvent).where(
                            UsageEvent.id.in_([item.id for item in reservations])
                        )
                    )
                )
                .scalars()
                .all()
            )
            outcomes = {row.id: row for row in rows}
            assert outcomes[reservations[0].id].provider_outcome == "success"
            assert outcomes[reservations[0].id].input_tokens == 12
            assert outcomes[reservations[1].id].provider_outcome == "failed"
            assert outcomes[reservations[1].id].error_code == "INTENT_AI_UPSTREAM_ERROR"

        # Two calls one minute apart straddle Shanghai midnight. Each belongs
        # to a different quota window, and the persisted timestamp must be the
        # exact instant used to choose that window.
        boundary_settings = _test_settings(daily_llm_call_limit=1)
        before_midnight = datetime(2099, 1, 1, 15, 59, tzinfo=UTC)
        at_midnight = datetime(2099, 1, 1, 16, 0, tzinfo=UTC)
        for instant in (before_midnight, at_midnight):
            boundary_reservations.append(
                await reserve_provider_call(
                    uuid4(),
                    "text",
                    "intent",
                    settings=boundary_settings,
                    now=instant,
                )
            )
        async with get_session_factory()() as session:
            boundary_rows = (
                (
                    await session.execute(
                        sa.select(UsageEvent).where(
                            UsageEvent.id.in_([item.id for item in boundary_reservations])
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert {row.created_at for row in boundary_rows} == {
            before_midnight,
            at_midnight,
        }
    finally:
        await _delete_rows(
            usage_ids=[
                old_id,
                *(item.id for item in reservations),
                *(item.id for item in boundary_reservations),
            ],
            conversation_ids=[],
        )


@pytest.mark.asyncio
async def test_postgres_asr_quota_is_independent_and_counts_failed_successful_reservations() -> (
    None
):
    _require_isolated_stack()
    settings = _test_settings(daily_llm_call_limit=1, daily_asr_seconds_limit=3)
    now = datetime.now(UTC)
    reservations = []
    try:
        intent = await reserve_provider_call(uuid4(), "text", "intent", settings=settings, now=now)
        reservations.append(intent)

        failed_asr = await reserve_provider_call(
            uuid4(), "voice", "asr", units=1, settings=settings, now=now
        )
        reservations.append(failed_asr)
        await finish_provider_call(failed_asr, "failed", error_code="ASR_TIMEOUT")

        successful_asr = await reserve_provider_call(
            uuid4(), "voice", "asr", units=2, settings=settings, now=now
        )
        reservations.append(successful_asr)
        await finish_provider_call(successful_asr, "success")

        with pytest.raises(AppError) as asr_exhausted:
            await reserve_provider_call(
                uuid4(), "voice", "asr", units=1, settings=settings, now=now
            )
        assert asr_exhausted.value.code == "DAILY_ASR_QUOTA_EXHAUSTED"

        with pytest.raises(AppError) as intent_exhausted:
            await reserve_provider_call(uuid4(), "text", "intent", settings=settings, now=now)
        assert intent_exhausted.value.code == "DAILY_LLM_QUOTA_EXHAUSTED"

        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(UsageEvent).where(
                            UsageEvent.id.in_([item.id for item in reservations])
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_id = {row.id: row for row in rows}
        assert by_id[failed_asr.id].provider_type == "asr"
        assert by_id[failed_asr.id].reserved_units == 1
        assert by_id[failed_asr.id].provider_outcome == "failed"
        assert by_id[successful_asr.id].provider_type == "asr"
        assert by_id[successful_asr.id].reserved_units == 2
        assert by_id[successful_asr.id].provider_outcome == "success"
        assert by_id[intent.id].provider_type == "intent"
        assert by_id[intent.id].reserved_units == 1
    finally:
        await _delete_rows(usage_ids=[item.id for item in reservations], conversation_ids=[])


@pytest.mark.asyncio
async def test_postgres_conversation_ttl_pending_clear_and_physical_delete() -> None:
    _require_isolated_stack()
    conversation_id = uuid4()
    expired_id = uuid4()
    try:
        await save_conversation_state(
            conversation_id,
            target_slug="vip",
            origin_slug="t-spawn",
            pending_options=["vip", "ct-spawn"],
        )
        loaded = await load_conversation_state(conversation_id)
        assert loaded == ConversationSnapshot(
            "vip",
            "t-spawn",
            "target",
            ["vip", "ct-spawn"],
            loaded.expires_at,
        )
        cleared = await clear_pending_state(conversation_id)
        assert cleared.conversation_id == conversation_id
        assert cleared.previous is not None
        assert cleared.previous.pending_options == ["vip", "ct-spawn"]
        after_clear = await load_conversation_state(conversation_id)
        assert after_clear is not None
        assert after_clear.pending_field is None
        assert after_clear.pending_options is None

        async with get_session_factory()() as session:
            async with session.begin():
                session.add(
                    ConversationState(
                        id=expired_id,
                        map="mirage",
                        grenade="smoke",
                        target_slug="vip",
                        origin_slug="t-spawn",
                        expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                )
        assert await load_conversation_state(expired_id) is None
        async with get_session_factory()() as session:
            assert await session.get(ConversationState, expired_id) is None
    finally:
        await _delete_rows(usage_ids=[], conversation_ids=[conversation_id, expired_id])


@pytest.mark.asyncio
async def test_postgres_selection_query_error_and_active_point_revalidation() -> None:
    """Exercise session transitions against the same PostgreSQL used by the API."""

    _require_isolated_stack()
    target, origin = await _insert_active_points()
    conversation_ids = [uuid4(), uuid4(), uuid4(), uuid4()]
    row = _local_row(target, origin)

    async def rows_loader(_session, **_kwargs):
        return [row]

    settings = _test_settings(ai_provider="disabled")
    providers = ProviderSet(DisabledIntentParser())
    try:
        # An invalid selection is a 409 and does not mutate the pending row.
        invalid_id = conversation_ids[0]
        await save_conversation_state(
            invalid_id,
            target_slug=None,
            origin_slug=origin.slug,
            pending_options=[target.slug],
        )
        invalid = SimpleNamespace(
            kind="selection",
            conversation_id=invalid_id,
            selection=SimpleNamespace(field="target", value="not-an-option"),
            input_mode="text",
        )
        with pytest.raises(AppError) as invalid_error:
            await run_search(
                _request(),
                invalid,
                settings=settings,
                providers=providers,
                catalog_loader=active_catalog,
                rows_loader=rows_loader,
                snapshot_factory=public_snapshot,
            )
        assert (invalid_error.value.status_code, invalid_error.value.code) == (
            409,
            "INVALID_OR_EXPIRED_CLARIFICATION",
        )
        invalid_state = await load_conversation_state(invalid_id)
        assert invalid_state is not None
        assert invalid_state.pending_options == [target.slug]
        assert invalid_state.origin_slug == origin.slug

        # A valid selection reaches a local row and clears pending state.
        selected_id = conversation_ids[1]
        await save_conversation_state(
            selected_id,
            target_slug=None,
            origin_slug=origin.slug,
            pending_options=[target.slug],
        )
        selected = SimpleNamespace(
            kind="selection",
            conversation_id=selected_id,
            selection=SimpleNamespace(field="target", value=target.slug),
            input_mode="text",
        )
        result = await run_search(
            _request(),
            selected,
            settings=settings,
            providers=providers,
            catalog_loader=active_catalog,
            rows_loader=rows_loader,
            snapshot_factory=public_snapshot,
        )
        assert result["type"] == "match"
        selected_state = await load_conversation_state(selected_id)
        assert selected_state is not None
        assert selected_state.pending_options is None
        assert selected_state.target_slug == target.slug
        assert selected_state.origin_slug == origin.slug

        # Archiving either active directory entity invalidates a stale
        # clarification before retrieval and leaves its session untouched.
        stale_id = conversation_ids[2]
        await save_conversation_state(
            stale_id,
            target_slug=None,
            origin_slug=origin.slug,
            pending_options=[target.slug],
        )
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    sa.update(Target).where(Target.id == target.id).values(is_active=False)
                )
        with pytest.raises(AppError) as archived_target_error:
            await run_search(
                _request(),
                SimpleNamespace(
                    kind="selection",
                    conversation_id=stale_id,
                    selection=SimpleNamespace(field="target", value=target.slug),
                    input_mode="text",
                ),
                settings=settings,
                providers=providers,
                catalog_loader=active_catalog,
                rows_loader=rows_loader,
                snapshot_factory=public_snapshot,
            )
        assert archived_target_error.value.code == "INVALID_OR_EXPIRED_CLARIFICATION"
        stale_state = await load_conversation_state(stale_id)
        assert stale_state is not None and stale_state.pending_options == [target.slug]

        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    sa.update(Target).where(Target.id == target.id).values(is_active=True)
                )
                await session.execute(
                    sa.update(Origin).where(Origin.id == origin.id).values(is_active=False)
                )
        origin_result = await run_search(
            _request(),
            SimpleNamespace(
                kind="selection",
                conversation_id=stale_id,
                selection=SimpleNamespace(field="target", value=target.slug),
                input_mode="text",
            ),
            settings=settings,
            providers=providers,
            catalog_loader=active_catalog,
            rows_loader=rows_loader,
            snapshot_factory=public_snapshot,
        )
        assert origin_result["type"] == "route_matches"
        assert [notice["code"] for notice in origin_result["notices"]] == ["ORIGIN_UNRESOLVED"]
        origin_state = await load_conversation_state(stale_id)
        assert origin_state is not None
        assert origin_state.pending_options is None
        assert origin_state.target_slug == target.slug
        assert origin_state.origin_slug is None

        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    sa.update(Origin).where(Origin.id == origin.id).values(is_active=True)
                )

        # A query clears pending at entry but a provider 5xx retains the old
        # normalized target/origin intent.
        error_id = conversation_ids[3]
        await save_conversation_state(
            error_id,
            target_slug=target.slug,
            origin_slug=origin.slug,
            pending_options=[target.slug],
        )

        class FailingIntent:
            async def parse(self, *_args, **_kwargs):
                raise ProviderFailure(502, "INTENT_AI_UPSTREAM_ERROR", "safe")

        query = SimpleNamespace(
            kind="query",
            conversation_id=error_id,
            text="无法识别的目标",
            input_mode="text",
        )
        with pytest.raises(ProviderFailure) as provider_error:
            await run_search(
                _request(),
                query,
                settings=settings,
                providers=ProviderSet(FailingIntent()),
                catalog_loader=active_catalog,
                rows_loader=rows_loader,
                snapshot_factory=public_snapshot,
            )
        assert provider_error.value.code == "INTENT_AI_UPSTREAM_ERROR"
        after_error = await load_conversation_state(error_id)
        assert after_error is not None
        assert after_error.pending_options is None
        assert after_error.target_slug == target.slug
        assert after_error.origin_slug == origin.slug
    finally:
        await _delete_rows(usage_ids=[], conversation_ids=conversation_ids)
        await _delete_points(target.id, origin.id)


@pytest.mark.asyncio
async def test_postgres_conversation_lock_busy_cancel_cleanup_and_parallel_ids() -> None:
    _require_isolated_stack()
    conversation_id = uuid4()
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock() -> None:
        async with conversation_lock(conversation_id, 12):
            started.set()
            await release.wait()

    first = asyncio.create_task(hold_lock())
    await started.wait()
    with pytest.raises(AppError) as busy:
        async with conversation_lock(conversation_id, 3):
            raise AssertionError("the same conversation must be busy")
    assert (busy.value.status_code, busy.value.code) == (409, "CONVERSATION_BUSY")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    # Cancellation releases the reference and lock so the next request can
    # acquire the same conversation id.
    async with conversation_lock(conversation_id, 3):
        pass

    active = 0
    maximum = 0
    guard = asyncio.Lock()

    async def parallel_hold(conversation: UUID) -> None:
        nonlocal active, maximum
        async with conversation_lock(conversation, 3):
            async with guard:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0.05)
            async with guard:
                active -= 1

    await asyncio.gather(parallel_hold(uuid4()), parallel_hold(uuid4()))
    assert maximum == 2
