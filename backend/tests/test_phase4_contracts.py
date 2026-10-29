from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.config import Settings
from app.core.errors import AppError
from app.schemas.agent import IntentExtraction
from app.schemas.public import SearchResponse
from app.services.local_search import CatalogEntity
from app.services.normalization import catalog_terms
from app.services.providers import (
    DisabledIntentParser,
    FakeIntentParser,
    ProviderFailure,
    QuotaReservation,
    provider_factory,
    shanghai_day_bounds,
)


def catalog(
    slug: str,
    name: str,
    *,
    alias: str | None = None,
    name_en: str | None = None,
) -> CatalogEntity:
    return CatalogEntity(
        uuid4(),
        slug,
        name,
        name_en,
        catalog_terms(name, name_en, [alias] if alias else []),
    )


def _test_settings(ai_provider: str = "fake") -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        postgres_password="smokeline",
        ai_provider=ai_provider,
    )


def test_intent_schema_is_strict_and_rejects_non_finite_confidence() -> None:
    payload = {
        "map": "mirage",
        "grenade": "smoke",
        "target_candidates": [{"slug": "vip", "confidence": 0.9}],
        "origin_candidates": [],
        "target_mentioned": True,
        "origin_mentioned": False,
        "is_follow_up": False,
        "explicitly_replaced_fields": [],
    }
    assert IntentExtraction.model_validate(payload).model_dump() == payload
    schema = IntentExtraction.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["IntentCandidate"]["additionalProperties"] is False
    with pytest.raises(ValidationError):
        IntentExtraction.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        IntentExtraction.model_validate(
            {**payload, "target_candidates": [{"slug": "vip", "confidence": float("nan")}]}
        )
    with pytest.raises(ValidationError):
        IntentExtraction.model_validate(
            {**payload, "explicitly_replaced_fields": ["target", "target"]}
        )
    with pytest.raises(ValidationError):
        IntentExtraction.model_validate(
            {key: value for key, value in payload.items() if key != "target_mentioned"}
        )
    assert (
        IntentExtraction.model_validate({**payload, "target_mentioned": False}).target_mentioned
        is False
    )
    with pytest.raises(ValidationError):
        IntentExtraction.model_validate({**payload, "target_mentioned": 1})


def test_provider_set_contains_only_intent_parser() -> None:
    from dataclasses import fields

    from app.services.providers import ProviderSet

    assert [field.name for field in fields(ProviderSet)] == ["intent"]


def test_public_union_rejects_retired_external_response() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SearchResponse).validate_python(
            {
                "type": "external_candidates",
                "conversation_id": "conversation-1",
                "candidates": [],
            }
        )


def test_compiled_graph_keeps_the_direct_target_clarification_edge() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    edges = {(edge.source, edge.target) for edge in graph._GRAPH.get_graph().edges}
    assert ("deterministic_parse", "build_clarification") in edges


@pytest.mark.asyncio
async def test_disabled_intent_provider_fails_without_returning_data() -> None:
    with pytest.raises(AppError) as intent_error:
        await DisabledIntentParser().parse("vip", {}, None)
    assert (intent_error.value.status_code, intent_error.value.code) == (
        503,
        "INTENT_AI_UNAVAILABLE",
    )


@pytest.mark.asyncio
async def test_fake_provider_contract_is_fixed_and_network_free() -> None:
    extraction = await FakeIntentParser().parse("从匪家封中路看人的窗口", {}, None)
    assert extraction.target_candidates[0].slug == "vip"
    assert extraction.origin_candidates[0].slug == "t-spawn"


def test_shanghai_quota_day_boundaries_are_utc_aware() -> None:
    before_midnight = shanghai_day_bounds(datetime(2026, 9, 7, 15, 59, tzinfo=UTC))
    at_midnight = shanghai_day_bounds(datetime(2026, 9, 7, 16, 0, tzinfo=UTC))
    assert before_midnight == (
        datetime(2026, 9, 6, 16, 0, tzinfo=UTC),
        datetime(2026, 9, 7, 16, 0, tzinfo=UTC),
    )
    assert at_midnight == (
        datetime(2026, 9, 7, 16, 0, tzinfo=UTC),
        datetime(2026, 9, 8, 16, 0, tzinfo=UTC),
    )


def test_graph_has_no_external_ranker_or_url_sanitizer() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    assert not hasattr(graph, "rank_external_results")
    assert not hasattr(graph, "sanitize_bilibili_url")


def test_decimal_candidate_thresholds_follow_contract() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    candidates = [
        type("Candidate", (), {"slug": "vip", "confidence": Decimal("0.90")})(),
        type("Candidate", (), {"slug": "a", "confidence": Decimal("0.70")})(),
    ]
    assert graph.candidate_decision(candidates)[:2] == ("unique", "vip")


def _dashscope_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        postgres_password="smokeline",
        ai_provider="dashscope",
        dashscope_api_key="test-key",
        dashscope_api_host="dashscope.aliyuncs.com",
    )


def test_dashscope_provider_exposes_intent_only() -> None:
    import app.services.dashscope_provider as dashscope

    assert not hasattr(dashscope, "DashScopeWebSearcher")
    assert not any(name.startswith("search_") for name in vars(dashscope))


@pytest.mark.asyncio
async def test_dashscope_intent_serializes_untrusted_catalog_and_finishes_usage(
    monkeypatch,
) -> None:
    from app.services.dashscope_provider import DashScopeIntentParser

    captured: dict[str, object] = {}
    outcomes: list[tuple[str, dict[str, object]]] = []
    reservation = QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))
    extraction = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[{"slug": "vip", "confidence": 0.92}],
        origin_candidates=[],
        target_mentioned=True,
        origin_mentioned=False,
        is_follow_up=False,
        explicitly_replaced_fields=[],
    )

    class FakeCompletions:
        async def parse(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(refusal=None, parsed=extraction),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=13, completion_tokens=5),
            )

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        return reservation

    async def finish(_item: QuotaReservation, outcome: str, **kwargs: object) -> None:
        outcomes.append((outcome, kwargs))

    parser = DashScopeIntentParser(
        settings=_dashscope_settings(),
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 5,
        reservation_fn=reserve,
        outcome_fn=finish,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    result = await parser.parse(
        "<untrusted>",
        {
            "targets": [{"slug": "vip", "name_zh": "VIP", "name_en": None, "aliases": []}],
            "origins": [],
        },
        {"target_slug": None, "origin_slug": None},
    )
    assert result == extraction
    payload = json.loads(captured["messages"][1]["content"])
    assert payload == {
        "catalog": {
            "origins": [],
            "targets": [{"aliases": [], "name_en": None, "name_zh": "VIP", "slug": "vip"}],
        },
        "previous_intent": {"origin_slug": None, "target_slug": None},
        "user_text": "<untrusted>",
    }
    timeout = captured["client_kwargs"]["timeout"]
    assert timeout.connect <= 1.5
    assert captured["client_kwargs"]["max_retries"] == 0
    assert "max_tokens" not in captured
    assert "max_completion_tokens" not in captured
    assert outcomes == [("success", {"input_tokens": 13, "output_tokens": 5, "error_code": None})]
    assert captured["closed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["no_choice", "length", "refusal", "missing", "extra", "bad_usage"],
)
async def test_dashscope_intent_rejects_bad_response_shapes_without_retry(
    monkeypatch, case: str
) -> None:
    from app.services.dashscope_provider import DashScopeIntentParser

    calls = 0
    outcomes: list[tuple[str, str | None]] = []
    reservation = QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))
    parsed: dict[str, object] = {
        "map": "mirage",
        "grenade": "smoke",
        "target_candidates": [{"slug": "vip", "confidence": 0.9}],
        "origin_candidates": [],
        "target_mentioned": True,
        "origin_mentioned": False,
        "is_follow_up": False,
        "explicitly_replaced_fields": [],
    }
    finish_reason = "stop"
    refusal = None
    choices: list[object]
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2)
    if case == "missing":
        parsed.pop("grenade")
    elif case == "extra":
        parsed["unexpected"] = True
    elif case == "length":
        finish_reason = "length"
    elif case == "refusal":
        refusal = "private refusal"
    elif case == "bad_usage":
        usage = SimpleNamespace(prompt_tokens=True, completion_tokens=2)
    choices = [
        SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(refusal=refusal, parsed=parsed),
        )
    ]
    if case == "no_choice":
        choices = []
    response = SimpleNamespace(choices=choices, usage=usage)

    class FakeCompletions:
        async def parse(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return response

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        return reservation

    async def finish(_item: QuotaReservation, outcome: str, **kwargs: object) -> None:
        outcomes.append((outcome, kwargs.get("error_code")))

    parser = DashScopeIntentParser(
        settings=_dashscope_settings(),
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 5,
        reservation_fn=reserve,
        outcome_fn=finish,
    )
    with pytest.raises(ProviderFailure) as error:
        await parser.parse("VIP", {"targets": [], "origins": []}, None)
    assert (error.value.status_code, error.value.code) == (502, "INTENT_AI_BAD_RESPONSE")
    assert "private refusal" not in error.value.message
    assert calls == 1
    assert outcomes == [("failed", "INTENT_AI_BAD_RESPONSE")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception_name", ["APIConnectionError", "AuthenticationError", "APIStatusError"]
)
async def test_dashscope_intent_maps_upstream_errors_without_retry(
    monkeypatch, exception_name: str
) -> None:
    from app.services.dashscope_provider import DashScopeIntentParser

    calls = 0
    outcomes: list[tuple[str, str | None]] = []
    reservation = QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))
    exception_type = type(exception_name, (Exception,), {})

    class FakeCompletions:
        async def parse(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise exception_type("private upstream detail")

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        return reservation

    async def finish(_item: QuotaReservation, outcome: str, **kwargs: object) -> None:
        outcomes.append((outcome, kwargs.get("error_code")))

    parser = DashScopeIntentParser(
        settings=_dashscope_settings(),
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 5,
        reservation_fn=reserve,
        outcome_fn=finish,
    )
    with pytest.raises(ProviderFailure) as error:
        await parser.parse("VIP", {"targets": [], "origins": []}, None)
    assert (error.value.status_code, error.value.code) == (502, "INTENT_AI_UPSTREAM_ERROR")
    assert "private upstream detail" not in error.value.message
    assert calls == 1
    assert outcomes == [("failed", "INTENT_AI_UPSTREAM_ERROR")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception_name",
    [
        "APIResponseValidationError",
        "ContentFilterFinishReasonError",
        "LengthFinishReasonError",
    ],
)
async def test_dashscope_intent_maps_sdk_parse_failures_to_bad_response(
    monkeypatch, exception_name: str
) -> None:
    from app.services.dashscope_provider import DashScopeIntentParser

    calls = 0
    outcomes: list[tuple[str, str | None]] = []
    reservation = QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))
    exception_type = type(exception_name, (Exception,), {})

    class FakeCompletions:
        async def parse(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise exception_type("private malformed response")

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        return reservation

    async def finish(_item: QuotaReservation, outcome: str, **kwargs: object) -> None:
        outcomes.append((outcome, kwargs.get("error_code")))

    parser = DashScopeIntentParser(
        settings=_dashscope_settings(),
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 5,
        reservation_fn=reserve,
        outcome_fn=finish,
    )
    with pytest.raises(ProviderFailure) as error:
        await parser.parse("VIP", {"targets": [], "origins": []}, None)
    assert (error.value.status_code, error.value.code) == (502, "INTENT_AI_BAD_RESPONSE")
    assert "private malformed response" not in error.value.message
    assert calls == 1
    assert outcomes == [("failed", "INTENT_AI_BAD_RESPONSE")]


@pytest.mark.asyncio
async def test_dashscope_intent_sdk_timeout_maps_to_504_and_failed_outcome(monkeypatch) -> None:
    from app.services.dashscope_provider import DashScopeIntentParser

    class APITimeoutError(Exception):
        pass

    class FakeCompletions:
        async def parse(self, **_kwargs: object) -> object:
            raise APITimeoutError("sdk timeout")

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))
    reservation = QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))
    outcomes: list[tuple[str, str | None]] = []

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        return reservation

    async def finish(_item: QuotaReservation, outcome: str, **kwargs: object) -> None:
        outcomes.append((outcome, kwargs.get("error_code")))

    parser = DashScopeIntentParser(
        settings=_dashscope_settings(),
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 5,
        reservation_fn=reserve,
        outcome_fn=finish,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    with pytest.raises(ProviderFailure) as error:
        await parser.parse("VIP", {"targets": [], "origins": []}, None)
    assert (error.value.status_code, error.value.code) == (504, "INTENT_AI_TIMEOUT")
    assert outcomes == [("failed", "INTENT_AI_TIMEOUT")]


@pytest.mark.asyncio
async def test_provider_deadline_and_catalog_limit_happen_before_reservation() -> None:
    from app.services.dashscope_provider import DashScopeIntentParser

    reservation_calls = 0

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        nonlocal reservation_calls
        reservation_calls += 1
        return QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))

    async def finish(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("finish must not run without reservation")

    settings = _dashscope_settings()
    parser = DashScopeIntentParser(
        settings=settings,
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 0.5,
        reservation_fn=reserve,
        outcome_fn=finish,
    )
    with pytest.raises(ProviderFailure) as timeout_error:
        await parser.parse("vip", {}, None)
    assert timeout_error.value.code == "INTENT_AI_TIMEOUT"

    parser = DashScopeIntentParser(
        settings=settings,
        request_id=uuid4().hex,
        input_mode="text",
        deadline=time.monotonic() + 5,
        reservation_fn=reserve,
        outcome_fn=finish,
    )
    with pytest.raises(AppError) as size_error:
        await parser.parse("x" * 20_000, {}, None)
    assert size_error.value.code == "INTENT_CATALOG_TOO_LARGE"
    assert reservation_calls == 0


@pytest.mark.asyncio
async def test_provider_rechecks_remaining_budget_after_reservation(
    monkeypatch,
) -> None:
    import app.services.dashscope_provider as dashscope

    now = 100.0
    reservation = QuotaReservation(uuid4(), uuid4(), "intent", Decimal("1"))
    outcomes: list[tuple[str, str | None]] = []
    provider_calls = 0

    monkeypatch.setattr(dashscope.time, "monotonic", lambda: now)

    async def reserve(_provider_type: str, _units: Decimal) -> QuotaReservation:
        nonlocal now
        now = 100.6
        return reservation

    async def finish(_item: QuotaReservation, outcome: str, **kwargs: object) -> None:
        outcomes.append((outcome, kwargs.get("error_code")))

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(500)

    common = {
        "settings": _dashscope_settings(),
        "request_id": uuid4().hex,
        "input_mode": "text",
        "deadline": 101.5,
        "reservation_fn": reserve,
        "outcome_fn": finish,
        "transport": httpx.MockTransport(handler),
    }
    parser = dashscope.DashScopeIntentParser(**common)
    with pytest.raises(ProviderFailure) as error:
        await parser.parse("VIP", {"targets": [], "origins": []}, None)
    assert (error.value.status_code, error.value.code) == (504, "INTENT_AI_TIMEOUT")
    assert provider_calls == 0
    assert outcomes == [("failed", "INTENT_AI_TIMEOUT")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_error",
    [
        OperationalError(
            "SELECT 1", {}, RuntimeError("connection refused"), connection_invalidated=True
        ),
        PoolTimeoutError("connection pool exhausted"),
    ],
)
async def test_conversation_maps_only_unavailable_db_errors(monkeypatch, database_error) -> None:
    graph = pytest.importorskip("app.services.agent_graph")

    @asynccontextmanager
    async def broken_session():
        raise database_error
        yield

    monkeypatch.setattr(graph, "get_session_factory", lambda: broken_session)
    with pytest.raises(AppError, match="数据库暂时不可用") as clear_error:
        await graph.clear_pending_state(uuid4())
    assert (clear_error.value.status_code, clear_error.value.code) == (
        503,
        "DATABASE_UNAVAILABLE",
    )
    with pytest.raises(AppError) as save_error:
        await graph.save_conversation_state(uuid4(), target_slug="vip", origin_slug=None)
    assert (save_error.value.status_code, save_error.value.code) == (
        503,
        "DATABASE_UNAVAILABLE",
    )


def test_provider_factory_keeps_fake_and_disabled_intent_network_free() -> None:
    base = {
        "app_env": "test",
        "database_url": "postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        "postgres_password": "smokeline",
    }
    disabled = provider_factory(settings=Settings(**base, ai_provider="disabled"))
    assert isinstance(disabled.intent, DisabledIntentParser)
    assert not hasattr(disabled, "web")
    fake = provider_factory(settings=Settings(**base, ai_provider="fake"))
    assert isinstance(fake.intent, FakeIntentParser)
    assert not hasattr(fake, "web")


def test_candidate_threshold_boundaries_are_decimal_exact() -> None:
    graph = pytest.importorskip("app.services.agent_graph")

    def candidate(slug: str, confidence: str):
        return SimpleNamespace(slug=slug, confidence=Decimal(confidence))

    assert graph.candidate_decision([candidate("vip", "0.85")])[:2] == ("unique", "vip")
    assert graph.candidate_decision([candidate("vip", "0.85"), candidate("window", "0.70")])[
        :2
    ] == ("unique", "vip")
    assert (
        graph.candidate_decision([candidate("vip", "0.85"), candidate("window", "0.71")])[0]
        == "clarification"
    )
    assert graph.candidate_decision([candidate("vip", "0.55")])[0] == "clarification"
    assert graph.candidate_decision([candidate("vip", "0.15")])[0] == "none"
    assert graph.candidate_decision(
        [candidate("vip", "0.80"), candidate("vip", "0.70"), candidate("window", "0.79")]
    )[3] == ["vip", "window"]


def test_graph_has_no_external_cache_helpers() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    assert not any(name.startswith("_read_") and "cache" in name for name in vars(graph))
    assert not any(name.startswith("_write_") and "cache" in name for name in vars(graph))


def _graph_entities() -> tuple[list[CatalogEntity], list[CatalogEntity]]:
    return (
        [
            catalog("vip", "VIP", alias="窗口"),
            catalog("ct-spawn", "警家"),
            catalog("bench", "长椅"),
        ],
        [catalog("t-spawn", "T 出生点"), catalog("a-ramp", "A1")],
    )


def _graph_context(graph, *, previous=None, providers=None):
    @asynccontextmanager
    async def snapshot():
        yield object()

    targets, origins = _graph_entities()

    async def load_catalog(_session):
        return targets, origins

    async def load_rows(_session, **_kwargs):
        return []

    request = SimpleNamespace(state=SimpleNamespace(request_id=uuid4().hex))
    body = SimpleNamespace(input_mode="text", kind="query", conversation_id=None)
    return graph.SearchContext(
        request,
        body,
        _test_settings(),
        providers=providers,
        previous=previous,
        catalog_loader=load_catalog,
        rows_loader=load_rows,
        snapshot_factory=snapshot,
        deadline=time.monotonic() + 10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown_target_slug",
        "unknown_origin_slug",
        "false_origin_mentioned",
        "false_target_mentioned",
    ],
)
async def test_graph_rejects_semantically_invalid_llm_catalog_output(case: str) -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    ctx = _graph_context(graph)
    extraction = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[
            {
                "slug": "outside-catalog" if case == "unknown_target_slug" else "vip",
                "confidence": 0.9,
            }
        ],
        origin_candidates=(
            [{"slug": "outside-catalog", "confidence": 0.9}]
            if case == "unknown_origin_slug"
            else (
                [{"slug": "t-spawn", "confidence": 0.9}] if case == "false_origin_mentioned" else []
            )
        ),
        target_mentioned=case != "false_target_mentioned",
        origin_mentioned=case == "unknown_origin_slug",
        is_follow_up=False,
        explicitly_replaced_fields=[],
    )
    with pytest.raises(ProviderFailure) as error:
        await graph._validate_llm_output({"llm_extraction": extraction}, ctx)
    assert (error.value.status_code, error.value.code) == (502, "INTENT_AI_BAD_RESPONSE")


@pytest.mark.asyncio
async def test_graph_llm_validation_carries_mentioned_flags_into_state() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    ctx = _graph_context(graph)
    extraction = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[],
        origin_candidates=[],
        target_mentioned=False,
        origin_mentioned=True,
        is_follow_up=False,
        explicitly_replaced_fields=["origin"],
    )
    validated = await graph._validate_llm_output(
        {"llm_extraction": extraction, "deterministic_result": {}}, ctx
    )
    assert validated["target_mentioned"] is False
    assert validated["origin_mentioned"] is True


@pytest.mark.asyncio
async def test_unknown_free_text_origin_does_not_adopt_provider_guess() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    ctx = _graph_context(graph)
    state = {
        "request_kind": "query",
        "raw_text": "我在水果摊，想封警家烟",
        "normalized_text": graph.normalize_text("我在水果摊，想封警家烟"),
        "previous_intent": None,
    }
    state.update(await graph._deterministic_parse(state, ctx))
    assert state["deterministic_result"]["route"] == "llm"
    state["llm_extraction"] = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[{"slug": "outside-catalog", "confidence": 0.99}],
        origin_candidates=[{"slug": "t-spawn", "confidence": 0.99}],
        target_mentioned=True,
        origin_mentioned=True,
        is_follow_up=False,
        explicitly_replaced_fields=["origin"],
    )

    state.update(await graph._validate_llm_output(state, ctx))
    extraction = state["llm_extraction"]
    assert extraction.target_candidates == []
    assert extraction.origin_candidates == []

    resolved = await graph._resolve_intent(state, ctx)
    assert resolved["resolved_intent"] == {
        "map": "mirage",
        "grenade": "smoke",
        "target_slug": "ct-spawn",
        "origin_slug": None,
    }
    assert [notice["code"] for notice in resolved["notices"]] == ["ORIGIN_UNRESOLVED"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_target", "expected_origin"),
    [
        ("那改成警家烟", "ct-spawn", "t-spawn"),
        ("从 A1 扔呢", "vip", "a-ramp"),
        ("那换成跳投", "vip", "t-spawn"),
    ],
)
async def test_graph_follow_up_examples_are_deterministic(
    text: str, expected_target: str, expected_origin: str
) -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    ctx = _graph_context(
        graph,
        previous=graph.ConversationSnapshot(
            "vip", "t-spawn", None, None, datetime.now(UTC) + timedelta(minutes=5)
        ),
    )
    state = {
        "request_kind": "query",
        "raw_text": text,
        "normalized_text": graph.normalize_text(text),
        "previous_intent": ctx.previous.intent,
    }
    deterministic = await graph._deterministic_parse(state, ctx)
    assert deterministic["deterministic_result"]["route"] == "resolve"
    state.update(deterministic)
    resolved = await graph._resolve_intent(state, ctx)
    assert resolved["resolved_intent"] == {
        "map": "mirage",
        "grenade": "smoke",
        "target_slug": expected_target,
        "origin_slug": expected_origin,
    }


@pytest.mark.asyncio
async def test_llm_unique_origin_survives_unknown_origin_location_cue() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    ctx = _graph_context(graph)
    text = "从某个未收录起点封 VIP"
    state = {
        "request_kind": "query",
        "raw_text": text,
        "normalized_text": graph.normalize_text(text),
        "previous_intent": None,
    }
    deterministic = await graph._deterministic_parse(state, ctx)
    assert deterministic["deterministic_result"]["route"] == "llm"
    state.update(deterministic)
    state["llm_extraction"] = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[{"slug": "vip", "confidence": 0.92}],
        origin_candidates=[{"slug": "t-spawn", "confidence": 0.91}],
        target_mentioned=True,
        origin_mentioned=True,
        is_follow_up=False,
        explicitly_replaced_fields=["origin"],
    )
    resolved = await graph._resolve_intent(state, ctx)
    assert resolved["resolved_intent"]["origin_slug"] == "t-spawn"
    assert resolved["field_confidences"]["origin"] == Decimal("0.91")
    assert resolved["notices"] == []


@pytest.mark.asyncio
async def test_follow_up_explicit_unresolved_origin_clears_old_origin() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    previous = graph.ConversationSnapshot(
        "vip", "t-spawn", None, None, datetime.now(UTC) + timedelta(minutes=5)
    )
    ctx = _graph_context(graph, previous=previous)
    text = "那从没收录的位置扔呢"
    state = {
        "request_kind": "query",
        "raw_text": text,
        "normalized_text": graph.normalize_text(text),
        "previous_intent": previous.intent,
    }
    deterministic = await graph._deterministic_parse(state, ctx)
    assert deterministic["deterministic_result"]["route"] == "llm"
    state.update(deterministic)
    state["llm_extraction"] = IntentExtraction(
        map="mirage",
        grenade="smoke",
        target_candidates=[],
        origin_candidates=[],
        target_mentioned=False,
        origin_mentioned=True,
        is_follow_up=True,
        explicitly_replaced_fields=["origin"],
    )
    resolved = await graph._resolve_intent(state, ctx)
    assert resolved["resolved_intent"]["target_slug"] == "vip"
    assert resolved["resolved_intent"]["origin_slug"] is None
    assert [notice["code"] for notice in resolved["notices"]] == ["ORIGIN_UNRESOLVED"]


@pytest.mark.asyncio
async def test_graph_local_miss_returns_local_not_found_without_provider_search() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target = catalog("bench", "长椅", name_en="Bench")
    origin = catalog("t-spawn", "T 出生点", name_en="T Spawn")

    async def no_rows(_session, **_kwargs):
        return []

    ctx = _graph_context(graph, providers=graph.ProviderSet(DisabledIntentParser()))
    ctx.rows_loader = no_rows
    ctx.targets = [target]
    ctx.origins = [origin]
    ctx.target = target
    ctx.origin = origin

    result = await graph._retrieve_local(
        {"resolved_intent": {"target_slug": "bench", "origin_slug": "t-spawn"}}, ctx
    )
    assert result["response_type"] == "not_found"
    assert result["error_code"] == "NO_LOCAL_RESULT"


@pytest.mark.asyncio
async def test_graph_intent_spy_is_called_once_with_catalog_only() -> None:
    graph = pytest.importorskip("app.services.agent_graph")

    class SpyIntent:
        def __init__(self):
            self.calls: list[tuple[str, object, object]] = []

        async def parse(self, text, catalog_payload, previous):
            self.calls.append((text, catalog_payload, previous))
            return IntentExtraction(
                map="mirage",
                grenade="smoke",
                target_candidates=[{"slug": "vip", "confidence": 0.92}],
                origin_candidates=[{"slug": "t-spawn", "confidence": 0.99}],
                target_mentioned=True,
                origin_mentioned=True,
                is_follow_up=False,
                explicitly_replaced_fields=["target", "origin"],
            )

    intent = SpyIntent()
    ctx = _graph_context(graph, providers=graph.ProviderSet(intent))
    state = {
        "raw_text": "从匪家封中路看人的窗口",
        "previous_intent": None,
        "deadline": time.monotonic() + 10,
    }
    parsed = await graph._llm_parse(state, ctx)
    assert isinstance(parsed["llm_extraction"], IntentExtraction)
    assert len(intent.calls) == 1
    text, catalog_payload, previous = intent.calls[0]
    assert text == "从匪家封中路看人的窗口"
    assert previous is None
    assert set(catalog_payload) == {"targets", "origins"}
    assert all(
        set(item) == {"slug", "name_zh", "name_en", "aliases"}
        for item in catalog_payload["targets"]
    )


@pytest.mark.asyncio
async def test_intent_failure_event_keeps_a_deterministic_target() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    request = SimpleNamespace(state=SimpleNamespace(request_id=uuid4().hex))
    targets, origins = _graph_entities()

    @asynccontextmanager
    async def snapshot():
        yield object()

    async def catalog_loader(_session):
        return targets, origins

    async def no_rows(_session, **_kwargs):
        return []

    class FailingIntent:
        async def parse(self, *_args, **_kwargs):
            raise ProviderFailure(502, "INTENT_AI_UPSTREAM_ERROR", "safe")

    body = SimpleNamespace(
        kind="query",
        text="我在一个没收录的位置，想封 VIP",
        conversation_id=None,
        input_mode="text",
    )
    with pytest.raises(ProviderFailure):
        await graph.run_search(
            request,
            body,
            providers=graph.ProviderSet(FailingIntent()),
            settings=_test_settings(),
            save_conversation=AsyncMock(),
            catalog_loader=catalog_loader,
            rows_loader=no_rows,
            snapshot_factory=snapshot,
        )
    assert request.state.public_event == {
        "response_type": None,
        "target_slug": "vip",
        "origin_slug": None,
        "error_code": "INTENT_AI_UPSTREAM_ERROR",
    }


@pytest.mark.asyncio
async def test_compiled_graph_local_and_no_result_paths_persist_once() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    target = catalog("bench", "长椅")
    origin = catalog("t-spawn", "T 出生点")
    local_target = catalog("vip", "VIP")
    request = SimpleNamespace(state=SimpleNamespace(request_id=uuid4().hex))
    saved: list[dict[str, object]] = []

    class NeverIntent:
        async def parse(self, *_args, **_kwargs):
            raise AssertionError("deterministic paths must not call Intent")

    async def save(_conversation_id, **values):
        saved.append(values)

    @asynccontextmanager
    async def snapshot():
        yield object()

    async def catalog_loader(_session):
        return [local_target, target], [origin]

    async def no_rows(_session, **_kwargs):
        return []

    local_lineup = SimpleNamespace(
        id=uuid4(),
        target_id=local_target.id,
        origin_id=origin.id,
        is_default=True,
        sort_order=1,
        setup_text="站位",
        aim_text="瞄点",
        throw_text="投掷",
        result_text="封烟",
        last_checked_at=None,
        game_version_note=None,
    )
    local_row = SimpleNamespace(
        lineup=local_lineup,
        target=SimpleNamespace(
            id=local_target.id,
            slug="vip",
            name_zh="VIP",
            map_x=50,
            map_y=25,
            sort_order=1,
            is_active=True,
        ),
        origin=SimpleNamespace(
            id=origin.id,
            slug="t-spawn",
            name_zh="T 出生点",
            map_x=20,
            map_y=80,
            sort_order=1,
            is_active=True,
        ),
        video=SimpleNamespace(
            relative_path="route.mp4",
            poster_relative_path="route.webp",
            checksum_sha256="a" * 64,
            duration_ms=1000,
        ),
    )

    async def local_rows(_session, **_kwargs):
        return [local_row]

    local_response = await graph.run_search(
        request,
        SimpleNamespace(
            kind="query", text="我在 T 出生点，想封 VIP", conversation_id=None, input_mode="text"
        ),
        providers=graph.ProviderSet(NeverIntent()),
        settings=_test_settings(),
        save_conversation=save,
        catalog_loader=catalog_loader,
        rows_loader=local_rows,
        snapshot_factory=snapshot,
    )
    assert local_response["type"] == "match"
    assert saved[-1] == {"target_slug": "vip", "origin_slug": "t-spawn", "pending_options": None}

    no_local_response = await graph.run_search(
        request,
        SimpleNamespace(kind="query", text="长椅", conversation_id=None, input_mode="text"),
        providers=graph.ProviderSet(NeverIntent()),
        settings=_test_settings(),
        save_conversation=save,
        catalog_loader=catalog_loader,
        rows_loader=no_rows,
        snapshot_factory=snapshot,
    )
    assert no_local_response["type"] == "not_found"
    assert no_local_response["reason"] == "NO_LOCAL_RESULT"
    assert saved[-1] == {"target_slug": "bench", "origin_slug": None, "pending_options": None}


def test_graph_has_no_external_retry_nodes() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    assert not hasattr(graph, "_search_bilibili_1")
    assert not hasattr(graph, "_search_bilibili_2")


def test_graph_does_not_expose_external_failure_nodes() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    assert not any("search_bilibili" in name for name in vars(graph))


@pytest.mark.asyncio
async def test_conversation_lock_busy_and_cancel_cleanup() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    conversation_id = uuid4()

    async def wait_for_lock() -> None:
        async with graph.conversation_lock(conversation_id, 1):
            return

    async with graph.conversation_lock(conversation_id, 1):
        holder = asyncio.create_task(wait_for_lock())
        await asyncio.sleep(0)
        with pytest.raises(AppError) as busy:
            async with graph.conversation_lock(conversation_id, 0.01):
                pass
        assert busy.value.code == "CONVERSATION_BUSY"
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
    await asyncio.sleep(0)
    assert conversation_id not in graph._CONVERSATION_ENTRIES


def test_graph_has_no_cache_write_node() -> None:
    graph = pytest.importorskip("app.services.agent_graph")
    assert not hasattr(graph, "_build_external_candidates")
    assert not any("cache" in name.lower() and name.startswith("_write") for name in vars(graph))
