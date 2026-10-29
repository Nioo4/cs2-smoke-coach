"""Focused v1.6.0 guards for retired external-search behavior.

These tests intentionally fail against the v1.5.2 implementation.  They keep
the removal contract executable while allowing the physical cache table and
historical database values to remain compatible.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.admin import MetricProviderUsage, MetricResponseCounts
from app.schemas.public import SearchNotFound, SearchResponse
from app.services import providers


def _union_models(annotation: object) -> set[type[object]]:
    args = get_args(annotation)
    if not args:
        return {annotation} if isinstance(annotation, type) else set()
    models: set[type[object]] = set()
    for item in args:
        models.update(_union_models(item))
    return models


def test_public_search_union_has_only_current_v16_response_types() -> None:
    model_names = {model.__name__ for model in _union_models(SearchResponse)}
    assert model_names == {
        "SearchMatch",
        "SearchRouteMatches",
        "SearchClarification",
        "SearchNotFound",
        "SearchOutOfScope",
    }

    valid = SearchNotFound(
        type="not_found",
        conversation_id="550e8400-e29b-41d4-a716-446655440000",
        reason="NO_LOCAL_RESULT",
        message="暂无符合该位置要求的站内路线，可换个起点或稍后再试。",
    )
    assert valid.reason == "NO_LOCAL_RESULT"
    with pytest.raises(ValidationError):
        TypeAdapter(SearchResponse).validate_python(
            {
                "type": "not_found",
                "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
                "reason": "NO_LOCAL_OR_EXTERNAL_RESULT",
                "message": "legacy",
            }
        )


def test_provider_set_and_quota_surface_have_no_web_search_runtime() -> None:
    assert not hasattr(providers, "WebSearcher")
    assert not hasattr(providers, "DisabledWebSearcher")
    assert not hasattr(providers, "FakeWebSearcher")
    assert [field.name for field in fields(providers.ProviderSet)] == ["intent"]

    settings_fields = set(providers.Settings.model_fields)
    assert "qwen_search_model" not in settings_fields
    assert "daily_web_search_call_limit" not in settings_fields


def test_graph_and_admin_runtime_do_not_reference_retired_search_or_cache_paths() -> None:
    source_root = Path(__file__).parents[1]
    graph_source = (source_root / "app/services/agent_graph.py").read_text(encoding="utf-8")
    admin_source = (source_root / "app/api/admin.py").read_text(encoding="utf-8")
    for source in (graph_source, admin_source):
        for forbidden in (
            "WebSearcher",
            "web_search",
            "external_candidates",
            "bilibili",
            "ExternalSearchCache",
            "_clear_external_cache",
        ):
            assert forbidden not in source


def test_metrics_have_only_intent_and_asr_current_fields() -> None:
    assert set(MetricResponseCounts.model_fields) == {
        "match",
        "route_matches",
        "clarification",
        "not_found",
        "out_of_scope",
    }
    assert set(MetricProviderUsage.model_fields) == {"intent", "asr"}


@pytest.mark.parametrize("relative_path", [".env.example", "compose.yml", "compose.test.yml"])
def test_active_configuration_has_no_web_search_settings(relative_path: str) -> None:
    project_root = Path(__file__).parents[2]
    config_path = project_root / relative_path
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
    else:
        # The backend test image intentionally contains only backend/.  Check
        # the copied runtime configuration sources instead of skipping this
        # guard merely because the host-level compose files are absent.
        backend_root = Path(__file__).parents[1]
        text = "\n".join(
            (
                (backend_root / "app/core/config.py").read_text(encoding="utf-8"),
                (backend_root / "app/services/providers.py").read_text(encoding="utf-8"),
            )
        )
    assert "QWEN_SEARCH_MODEL" not in text
    assert "DAILY_WEB_SEARCH_CALL_LIMIT" not in text
