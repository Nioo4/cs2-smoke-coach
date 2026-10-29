"""The single controlled local search graph for Phase 4."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID, uuid4

import sqlalchemy as sa
from langgraph.graph import END, START, StateGraph

from app.core.config import Settings, get_settings
from app.core.errors import AppError, database_is_unavailable
from app.db.models import ConversationState
from app.db.session import get_session_factory
from app.schemas.agent import IntentExtraction
from app.services.local_search import (
    CatalogEntity,
    PublicRow,
    active_catalog,
    available_rows,
    choose_group,
    detail_from_row,
    group_rows,
    public_snapshot,
    route_choice,
    route_sort_key,
)
from app.services.normalization import matching_entity_spans, normalize_text, term_in_text
from app.services.providers import (
    ProviderFailure,
    ProviderSet,
    provider_factory,
)

_SUPPORTED_MAP_TERMS = {"mirage", "荒漠迷城", "米垃圾"}
_SUPPORTED_GRENADE_TERMS = {"烟", "烟雾", "烟雾弹", "smoke"}
_OUT_OF_SCOPE_MAP_TERMS = {
    "dust2",
    "dust 2",
    "炙热沙城",
    "inferno",
    "炼狱小镇",
    "nuke",
    "核子危机",
    "ancient",
    "远古遗迹",
    "anubis",
    "阿努比斯",
    "overpass",
    "死亡游乐园",
    "train",
    "列车停放站",
    "vertigo",
    "殒命大厦",
}
_OUT_OF_SCOPE_GRENADE_TERMS = {
    "闪光弹",
    "闪光",
    "flash",
    "燃烧弹",
    "燃烧瓶",
    "molotov",
    "incendiary",
    "高爆手雷",
    "高爆",
    "he grenade",
    "诱饵弹",
    "decoy",
}
_ORIGIN_CUES = ("从", "在", "出生", "起点", "站在", "位置")
_FOLLOW_UP_WORDS = ("那", "改成", "换成", "呢")
_TEACHING_SKELETON_WORDS = {
    "我",
    "想",
    "要",
    "请",
    "帮",
    "找",
    "查",
    "看",
    "学",
    "一下",
    "从",
    "在",
    "怎么",
    "如何",
    "丢",
    "扔",
    "投",
    "投掷",
    "封",
    "封烟",
    "教学",
    "教程",
    "视频",
    "路线",
    "有哪些",
    "有什么",
    "起手",
    "都",
    "这儿",
    "能",
    "哪些",
}
_EXPLICIT_UNKNOWN_TARGET_CUES = ("不存在", "未收录", "未知", "没有这个", "没这个")
_TARGET_WORDS = ("点", "位置", "目标")


class AgentState(TypedDict, total=False):
    request_id: str
    conversation_id: str
    request_kind: str
    raw_text: str
    normalized_text: str
    selected_target_slug: str | None
    previous_intent: dict[str, object] | None
    resolved_intent: dict[str, object] | None
    deterministic_result: dict[str, object]
    llm_extraction: IntentExtraction | None
    field_confidences: dict[str, Decimal]
    target_candidates: list[str]
    target_mentioned: bool
    origin_mentioned: bool
    origin_unresolved: bool
    notices: list[dict[str, str]]
    local_lineups: list[dict[str, object]]
    response_type: str | None
    error_code: str | None
    query_mode: str
    deadline: float


@dataclass(slots=True)
class ConversationSnapshot:
    target_slug: str | None
    origin_slug: str | None
    pending_field: str | None
    pending_options: list[str] | None
    expires_at: datetime

    @property
    def intent(self) -> dict[str, object] | None:
        if self.target_slug is None and self.origin_slug is None:
            return None
        return {
            "map": "mirage",
            "grenade": "smoke",
            "target_slug": self.target_slug,
            "origin_slug": self.origin_slug,
        }


@dataclass(slots=True)
class PendingClearResult:
    conversation_id: UUID
    previous: ConversationSnapshot | None


@dataclass(slots=True)
class SearchContext:
    request: Any
    body: Any
    settings: Settings
    providers: ProviderSet | None = None
    response: dict[str, object] | None = None
    targets: list[CatalogEntity] = field(default_factory=list)
    origins: list[CatalogEntity] = field(default_factory=list)
    rows: list[PublicRow] = field(default_factory=list)
    previous: ConversationSnapshot | None = None
    target: CatalogEntity | None = None
    origin: CatalogEntity | None = None
    deadline: float | None = None
    pending_options: list[str] | None = None
    clear_pending: Callable[[UUID], Awaitable[PendingClearResult | UUID]] | None = None
    load_conversation: Callable[[UUID, bool], Awaitable[ConversationSnapshot | None]] | None = None
    save_conversation: Callable[..., Awaitable[None]] | None = None
    catalog_loader: Callable[[Any], Awaitable[tuple[list[CatalogEntity], list[CatalogEntity]]]] = (
        active_catalog
    )
    rows_loader: Callable[..., Awaitable[list[PublicRow]]] = available_rows
    snapshot_factory: Callable[[], Any] = public_snapshot
    conversation_id: UUID | None = None
    target_spans: list[tuple[str, int, int]] = field(default_factory=list)
    origin_spans: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass(slots=True)
class _ConversationEntry:
    lock: asyncio.Lock
    refs: int = 0


_CONVERSATION_ENTRIES: dict[UUID, _ConversationEntry] = {}
_CONVERSATION_GUARD = asyncio.Lock()


@asynccontextmanager
async def conversation_lock(conversation_id: UUID, remaining: float) -> AsyncIterator[None]:
    """Serialize an entire search by conversation id and clean refs on cancel."""

    async with _CONVERSATION_GUARD:
        entry = _CONVERSATION_ENTRIES.setdefault(
            _conversation_key(conversation_id), _ConversationEntry(asyncio.Lock())
        )
        entry.refs += 1
    acquired = False
    try:
        try:
            async with asyncio.timeout(min(2.0, max(0.0, remaining))):
                await entry.lock.acquire()
                acquired = True
        except TimeoutError as exc:
            raise AppError(409, "CONVERSATION_BUSY", "该会话正在处理中，请稍后重试。") from exc
        yield
    finally:
        if acquired:
            entry.lock.release()
        async with _CONVERSATION_GUARD:
            entry.refs -= 1
            if entry.refs <= 0 and not entry.lock.locked():
                _CONVERSATION_ENTRIES.pop(_conversation_key(conversation_id), None)


def _conversation_key(value: UUID) -> UUID:
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _snapshot_from_row(row: ConversationState) -> ConversationSnapshot:
    return ConversationSnapshot(
        target_slug=row.target_slug,
        origin_slug=row.origin_slug,
        pending_field=row.pending_field,
        pending_options=list(row.pending_options) if row.pending_options else None,
        expires_at=_as_utc(row.expires_at),
    )


def _map_database_unavailable(exc: Exception) -> None:
    """Map connection failures while preserving SQL/constraint defects."""

    if isinstance(exc, (sa.exc.DBAPIError, sa.exc.TimeoutError)) and database_is_unavailable(exc):
        raise AppError(503, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试。") from None


async def load_conversation_state(
    conversation_id: UUID,
    delete_expired: bool = True,
) -> ConversationSnapshot | None:
    now = datetime.now(UTC)
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                row = await session.get(
                    ConversationState, conversation_id, with_for_update=delete_expired
                )
                if row is None:
                    return None
                if _as_utc(row.expires_at) <= now:
                    if delete_expired:
                        await session.delete(row)
                    return None
                return _snapshot_from_row(row)
    except Exception as exc:
        _map_database_unavailable(exc)
        raise


async def clear_pending_state(conversation_id: UUID) -> PendingClearResult:
    """Clear pending target at query start while retaining the old intent."""

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=30)
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                row = await session.get(ConversationState, conversation_id, with_for_update=True)
                if row is None:
                    return PendingClearResult(uuid4(), None)
                if _as_utc(row.expires_at) <= now:
                    await session.delete(row)
                    return PendingClearResult(uuid4(), None)
                previous = _snapshot_from_row(row)
                row.pending_field = None
                row.pending_options = None
                row.expires_at = expires_at
                return PendingClearResult(conversation_id, previous)
    except Exception as exc:
        _map_database_unavailable(exc)
        raise


async def save_conversation_state(
    conversation_id: UUID,
    *,
    target_slug: str | None,
    origin_slug: str | None,
    pending_options: list[str] | None = None,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                row = await session.get(ConversationState, conversation_id, with_for_update=True)
                if row is None:
                    row = ConversationState(id=conversation_id, expires_at=expires_at)
                    session.add(row)
                row.map = "mirage"
                row.grenade = "smoke"
                row.target_slug = target_slug
                row.origin_slug = origin_slug
                row.pending_field = "target" if pending_options else None
                row.pending_options = pending_options
                row.expires_at = expires_at
    except Exception as exc:
        _map_database_unavailable(exc)
        raise


def _intent(target: str | None, origin: str | None) -> dict[str, object]:
    return {"map": "mirage", "grenade": "smoke", "target_slug": target, "origin_slug": origin}


def _query_mode(target: str | None, origin: str | None) -> str:
    if target is not None and origin is not None:
        return "exact"
    if target is not None:
        return "target_only"
    if origin is not None:
        return "origin_only"
    return "catalog"


def notice_origin_unresolved() -> dict[str, str]:
    return {
        "code": "ORIGIN_UNRESOLVED",
        "message": "没有识别出你说的起点，先展示可到达该目标的起点。",
    }


def scope_is_supported(text: str) -> bool:
    normalized = normalize_text(text)
    return not any(
        term_in_text(normalized, term)
        for term in _OUT_OF_SCOPE_MAP_TERMS | _OUT_OF_SCOPE_GRENADE_TERMS
    )


def _contains_follow_up(text: str) -> bool:
    return any(term in text for term in _FOLLOW_UP_WORDS)


def _contains_origin_cue(text: str) -> bool:
    # Chinese location cue "在" is a function word, but the same character in
    # "存在/不存在" is not an Origin signal. Remove that lexical occurrence
    # before applying the small cue set so an unknown Target stays a Target.
    without_exists = text.replace("存在", "")
    return any(cue in text for cue in _ORIGIN_CUES if cue != "在") or "在" in without_exists


def _remove_term(text: str, term: str) -> str:
    normalized = normalize_text(term)
    if not normalized:
        return text
    if normalized.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
        return re.sub(pattern, " ", text)
    return text.replace(normalized, " ")


def _is_teaching_skeleton(
    text: str,
    target_spans: Sequence[tuple[str, int, int]],
    origin_spans: Sequence[tuple[str, int, int]],
) -> bool:
    """Recognize only the small catalog-browse grammar from Spec 6.3.

    This is deliberately a request-shape check, not a throw/action parser. A
    remaining unknown location keeps the request on the Intent path so an
    explicit unknown Target can become NO_TARGET instead of broadening.
    """

    chars = list(text)
    for _slug, start, end in (*target_spans, *origin_spans):
        for index in range(start, end):
            chars[index] = " "
    remaining = "".join(chars)
    terms = _TEACHING_SKELETON_WORDS | _SUPPORTED_MAP_TERMS | _SUPPORTED_GRENADE_TERMS
    for term in sorted(terms, key=lambda value: (-len(normalize_text(value)), value)):
        remaining = _remove_term(remaining, term)
    return not normalize_text(remaining)


def _catalog_payload(entities: Sequence[CatalogEntity]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for entity in sorted(entities, key=lambda item: (item.sort_order, item.name_zh, str(item.id))):
        aliases = sorted(
            {term.value for term in entity.terms if term.source == "alias"},
            key=lambda value: normalize_text(value),
        )
        name_en = entity.name_en or None
        result.append(
            {
                "slug": entity.slug,
                "name_zh": entity.name_zh,
                "name_en": name_en,
                "aliases": aliases,
            }
        )
    return result


def _rank_candidates(candidates: Sequence[Any]) -> list[tuple[str, Decimal]]:
    best: dict[str, Decimal] = {}
    for candidate in candidates:
        slug = getattr(candidate, "slug", None)
        confidence = getattr(candidate, "confidence", None)
        if not isinstance(slug, str) or isinstance(confidence, bool):
            continue
        try:
            value = Decimal(str(confidence))
        except Exception:
            continue
        if not value.is_finite() or value < 0 or value > 1:
            continue
        if slug not in best or value > best[slug]:
            best[slug] = value
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))


def candidate_decision(
    candidates: Sequence[Any],
) -> tuple[str, str | None, Decimal | None, list[str]]:
    """Return (kind, selected_slug, confidence, clarification_options)."""

    ranked = _rank_candidates(candidates)
    if not ranked:
        return "none", None, None, []
    first_slug, first_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else None
    unique = first_score >= Decimal("0.85") and (
        second_score is None or first_score - second_score >= Decimal("0.15")
    )
    if unique:
        return "unique", first_slug, first_score, []
    if first_score >= Decimal("0.55"):
        return (
            "clarification",
            None,
            None,
            [slug for slug, score in ranked if score >= Decimal("0.55")][:6],
        )
    return "none", None, None, []


def _runtime_context(runtime: Any) -> SearchContext:
    return runtime.context if hasattr(runtime, "context") else runtime


async def _route_request(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    body = ctx.body
    request_id = getattr(ctx.request.state, "request_id", uuid4().hex)
    ctx.request.state.public_input_mode = (
        "text" if getattr(body, "kind", None) == "selection" else body.input_mode
    )
    if getattr(body, "kind", None) == "selection":
        conversation_id = body.conversation_id
        ctx.previous = await (ctx.load_conversation or load_conversation_state)(
            conversation_id, False
        )
        if (
            ctx.previous is None
            or ctx.previous.expires_at <= datetime.now(UTC)
            or ctx.previous.pending_field != "target"
            or not ctx.previous.pending_options
            or body.selection.value not in ctx.previous.pending_options
        ):
            _set_event(ctx, error_code="INVALID_OR_EXPIRED_CLARIFICATION")
            raise AppError(
                409, "INVALID_OR_EXPIRED_CLARIFICATION", "这次目标选择已失效，请重新搜索。"
            )
        return {
            "request_id": request_id,
            "conversation_id": str(conversation_id),
            "request_kind": "selection",
            "raw_text": body.selection.value,
            "selected_target_slug": body.selection.value,
            "previous_intent": ctx.previous.intent,
        }
    conversation_id = body.conversation_id or ctx.conversation_id or uuid4()
    if body.conversation_id is not None:
        clear_result = await (ctx.clear_pending or clear_pending_state)(conversation_id)
        if isinstance(clear_result, PendingClearResult):
            conversation_id = clear_result.conversation_id
            ctx.previous = clear_result.previous
        else:
            conversation_id = clear_result
            ctx.previous = await (ctx.load_conversation or load_conversation_state)(
                conversation_id, False
            )
    return {
        "request_id": request_id,
        "conversation_id": str(conversation_id),
        "request_kind": "query",
        "raw_text": body.text,
        "selected_target_slug": None,
        "previous_intent": ctx.previous.intent if ctx.previous else None,
    }


async def _normalize(state: AgentState, _ctx: SearchContext) -> dict[str, object]:
    return {"normalized_text": normalize_text(state.get("raw_text", ""))}


async def _deterministic_parse(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    if state["request_kind"] == "query" and not scope_is_supported(state["raw_text"]):
        return {"deterministic_result": {"route": "unsupported"}}
    async with ctx.snapshot_factory() as session:
        ctx.targets, ctx.origins = await ctx.catalog_loader(session)
        ctx.rows = await ctx.rows_loader(session)
    if ctx.previous is not None:
        active_target_slugs = {item.slug for item in ctx.targets}
        active_origin_slugs = {item.slug for item in ctx.origins}
        ctx.previous = ConversationSnapshot(
            target_slug=(
                ctx.previous.target_slug
                if ctx.previous.target_slug in active_target_slugs
                else None
            ),
            origin_slug=(
                ctx.previous.origin_slug
                if ctx.previous.origin_slug in active_origin_slugs
                else None
            ),
            pending_field=None,
            pending_options=None,
            expires_at=ctx.previous.expires_at,
        )
    text = state["normalized_text"]
    if state["request_kind"] == "selection":
        target_matches = [state["selected_target_slug"]]
        origin_matches: list[str] = []
        spans = {"target": [], "origin": []}
    else:
        tagged = [(f"target:{entity.slug}", entity.terms) for entity in ctx.targets] + [
            (f"origin:{entity.slug}", entity.terms) for entity in ctx.origins
        ]
        spans = {"target": [], "origin": []}
        for identifier, start, end, _term in matching_entity_spans(text, tagged):
            field, slug = identifier.split(":", 1)
            spans[field].append((slug, start, end))
        target_matches = list(dict.fromkeys(item[0] for item in spans["target"]))
        origin_matches = list(dict.fromkeys(item[0] for item in spans["origin"]))
        target_order = {item.slug: item for item in ctx.targets}
        origin_order = {item.slug: item for item in ctx.origins}
        target_matches.sort(
            key=lambda slug: (
                target_order[slug].sort_order,
                target_order[slug].name_zh,
                str(target_order[slug].id),
            )
        )
        origin_matches.sort(
            key=lambda slug: (
                origin_order[slug].sort_order,
                origin_order[slug].name_zh,
                str(origin_order[slug].id),
            )
        )
    ctx.target_spans = spans["target"]
    ctx.origin_spans = spans["origin"]
    target_intervals = {(start, end) for _slug, start, end in spans["target"]}
    origin_intervals = {(start, end) for _slug, start, end in spans["origin"]}
    cross_field = bool(target_intervals & origin_intervals)
    if not cross_field:
        ctx.target = (
            next((item for item in ctx.targets if item.slug == target_matches[0]), None)
            if len(target_matches) == 1
            else None
        )
        ctx.origin = (
            next((item for item in ctx.origins if item.slug == origin_matches[0]), None)
            if len(origin_matches) == 1
            else None
        )
    follow_up = bool(ctx.previous and _contains_follow_up(text))
    location_cue = _contains_origin_cue(text)
    explicit_unknown_target = (
        not target_matches
        and not location_cue
        and any(cue in text for cue in _EXPLICIT_UNKNOWN_TARGET_CUES)
        and any(word in text for word in _TARGET_WORDS)
    )
    only_follow_up_action = (
        follow_up and not target_matches and not origin_matches and not location_cue
    )
    follow_up_resolvable = (
        follow_up
        and len(target_matches) <= 1
        and len(origin_matches) <= 1
        and (bool(target_matches) or bool(origin_matches) or not location_cue)
    )
    teaching_skeleton = _is_teaching_skeleton(
        text,
        spans["target"],
        spans["origin"],
    )
    needs_llm = bool(
        state["request_kind"] == "query"
        and not target_matches
        and not follow_up_resolvable
        and not teaching_skeleton
        and not explicit_unknown_target
        or cross_field
        or (target_matches and not origin_matches and location_cue)
        or (follow_up and not only_follow_up_action and not follow_up_resolvable)
    )
    direct_target_clarification = len(target_matches) > 1 and not cross_field
    clarification_origin: str | None = None
    if direct_target_clarification:
        if len(origin_matches) == 1:
            clarification_origin = origin_matches[0]
        elif not origin_matches and not location_cue and follow_up and ctx.previous is not None:
            clarification_origin = ctx.previous.origin_slug
        ctx.pending_options = target_matches[:6]
    return {
        "deterministic_result": {
            "route": "llm" if needs_llm else "resolve",
            "target_matches": target_matches,
            "origin_matches": origin_matches,
            "cross_field": cross_field,
            "follow_up": follow_up,
            "location_cue": location_cue,
            "target_intervals": target_intervals,
            "origin_intervals": origin_intervals,
            "teaching_skeleton": teaching_skeleton,
            "explicit_unknown_target": explicit_unknown_target,
        },
        "resolved_intent": (
            _intent(None, clarification_origin) if direct_target_clarification else None
        ),
        "field_confidences": ({"origin": Decimal("1")} if clarification_origin is not None else {}),
        "target_candidates": target_matches[:6],
        "target_mentioned": bool(target_matches) or explicit_unknown_target,
        "origin_mentioned": bool(origin_matches),
        "origin_unresolved": bool(location_cue and len(origin_matches) != 1)
        or len(origin_matches) > 1,
        "previous_intent": ctx.previous.intent if ctx.previous else None,
    }


def _get_providers(ctx: SearchContext) -> ProviderSet:
    if ctx.providers is None:
        request_id = getattr(ctx.request.state, "request_id", uuid4().hex)
        ctx.providers = provider_factory(
            settings=ctx.settings,
            request_id=request_id,
            input_mode=getattr(ctx.body, "input_mode", "text"),
            deadline=ctx.deadline,
        )
    return ctx.providers


async def _llm_parse(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    ctx.deadline = state["deadline"]
    providers = _get_providers(ctx)
    extraction = await providers.intent.parse(
        state["raw_text"],
        {
            "targets": _catalog_payload(ctx.targets),
            "origins": _catalog_payload(ctx.origins),
        },
        state.get("previous_intent"),
    )
    return {"llm_extraction": extraction}


async def _validate_llm_output(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    extraction = state.get("llm_extraction")
    if not isinstance(extraction, IntentExtraction):
        raise ProviderFailure(502, "INTENT_AI_BAD_RESPONSE", "意图解析服务返回格式异常。")
    if not extraction.target_mentioned and extraction.target_candidates:
        raise ProviderFailure(502, "INTENT_AI_BAD_RESPONSE", "意图解析服务返回格式异常。")
    if not extraction.origin_mentioned and extraction.origin_candidates:
        raise ProviderFailure(502, "INTENT_AI_BAD_RESPONSE", "意图解析服务返回格式异常。")

    result = state.get("deterministic_result", {})
    target_matches = list(result.get("target_matches", []))
    origin_matches = list(result.get("origin_matches", []))
    cross_field = bool(result.get("cross_field"))
    updates: dict[str, object] = {}
    if not cross_field and len(target_matches) == 1:
        updates["target_candidates"] = []
    if not cross_field and (
        len(origin_matches) == 1
        or (len(target_matches) == 1 and not origin_matches and result.get("location_cue"))
    ):
        # In the unknown-Origin probe, the model only decides whether an Origin
        # was mentioned.  A candidate without a deterministic catalog match is
        # a guess and must not silently narrow the player's result.
        updates["origin_candidates"] = []
    if updates:
        extraction = extraction.model_copy(update=updates)

    target_slugs = {item.slug for item in ctx.targets}
    origin_slugs = {item.slug for item in ctx.origins}
    if any(item.slug not in target_slugs for item in extraction.target_candidates) or any(
        item.slug not in origin_slugs for item in extraction.origin_candidates
    ):
        raise ProviderFailure(502, "INTENT_AI_BAD_RESPONSE", "意图解析服务返回格式异常。")
    return {
        "llm_extraction": extraction,
        "target_mentioned": bool(state.get("target_mentioned")) or extraction.target_mentioned,
        "origin_mentioned": bool(state.get("origin_mentioned")) or extraction.origin_mentioned,
    }


async def _validate_pending_target(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    # The selection was validated against the pending row in route_request.
    # Re-read the active directory before resolving it so an archived target or
    # origin cannot be revived by a stale clarification response.
    async with ctx.snapshot_factory() as session:
        ctx.targets, ctx.origins = await ctx.catalog_loader(session)
        ctx.rows = await ctx.rows_loader(session)
    target_slug = state["selected_target_slug"]
    ctx.target = next((item for item in ctx.targets if item.slug == target_slug), None)
    origin_slug = ctx.previous.origin_slug if ctx.previous else None
    ctx.origin = (
        next((item for item in ctx.origins if item.slug == origin_slug), None)
        if origin_slug
        else None
    )
    if ctx.target is None:
        raise AppError(409, "INVALID_OR_EXPIRED_CLARIFICATION", "这次目标选择已失效，请重新搜索。")
    notices = [notice_origin_unresolved()] if origin_slug and ctx.origin is None else []
    return {
        "deterministic_result": {
            "route": "resolve",
            "target_matches": [target_slug],
            "origin_matches": [origin_slug] if origin_slug else [],
            "follow_up": False,
        },
        "resolved_intent": _intent(target_slug, origin_slug if ctx.origin else None),
        "previous_intent": ctx.previous.intent if ctx.previous else None,
        "notices": notices,
        "origin_unresolved": bool(notices),
    }


async def _apply_target(state: AgentState, _ctx: SearchContext) -> dict[str, object]:
    return {"field_confidences": {"target": Decimal("1"), "origin": Decimal("1")}}


async def _resolve_intent(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    if state["request_kind"] == "selection":
        intent = state.get("resolved_intent")
        if not isinstance(intent, dict) or not isinstance(intent.get("target_slug"), str):
            return {"response_type": "not_found", "error_code": "NO_TARGET"}
        return {
            "resolved_intent": intent,
            "field_confidences": {
                "target": Decimal("1"),
                **({"origin": Decimal("1")} if intent.get("origin_slug") else {}),
            },
            "notices": state.get("notices", []),
            "origin_unresolved": bool(state.get("origin_unresolved", False)),
        }
    result = state.get("deterministic_result", {})
    if result.get("route") == "unsupported":
        return {"response_type": "out_of_scope"}
    extraction = state.get("llm_extraction")
    previous = ctx.previous.intent if ctx.previous else None
    target_matches = list(result.get("target_matches", []))
    origin_matches = list(result.get("origin_matches", []))
    target_slug: str | None = (
        None
        if result.get("cross_field")
        else (target_matches[0] if len(target_matches) == 1 else None)
    )
    origin_slug: str | None = (
        None
        if result.get("cross_field")
        else (origin_matches[0] if len(origin_matches) == 1 else None)
    )
    field_confidences: dict[str, Decimal] = {}
    if target_slug:
        field_confidences["target"] = Decimal("1")
    if origin_slug:
        field_confidences["origin"] = Decimal("1")
    notices: list[dict[str, str]] = []
    if extraction is not None:
        if extraction.map == "unsupported" or extraction.grenade == "unsupported":
            return {"response_type": "out_of_scope"}
        if origin_slug is None:
            decision, selected, confidence, _options = candidate_decision(
                extraction.origin_candidates
            )
            if decision == "unique":
                origin_slug = selected
                field_confidences["origin"] = confidence or Decimal("1")
            elif extraction.origin_mentioned:
                notices.append(notice_origin_unresolved())
        if target_slug is None:
            decision, selected, confidence, options = candidate_decision(
                extraction.target_candidates
            )
            if decision == "clarification":
                # Intent runs against a snapshot that may become stale while
                # the provider is in flight.  Re-read the active directory
                # before exposing/persisting pending target slugs; an
                # archived Target must never become a selectable option, and
                # an archived Origin must not be carried into the session.
                async with ctx.snapshot_factory() as session:
                    ctx.targets, ctx.origins = await ctx.catalog_loader(session)
                active_target_slugs = {item.slug for item in ctx.targets}
                active_origin_slugs = {item.slug for item in ctx.origins}
                ctx.pending_options = [slug for slug in options if slug in active_target_slugs]
                if not ctx.pending_options:
                    return {
                        "response_type": "not_found",
                        "error_code": "NO_TARGET",
                        "resolved_intent": _intent(None, None),
                        "field_confidences": {},
                        "notices": [],
                        "target_candidates": [],
                    }
                if origin_slug is not None and origin_slug not in active_origin_slugs:
                    origin_slug = None
                    field_confidences.pop("origin", None)
                return {
                    "response_type": "clarification",
                    "resolved_intent": _intent(None, origin_slug),
                    "field_confidences": ({"origin": Decimal("1")} if origin_slug else {}),
                    "notices": notices,
                    "target_candidates": ctx.pending_options,
                }
            if decision == "unique":
                target_slug = selected
                field_confidences["target"] = confidence or Decimal("1")

    follow_up = bool(result.get("follow_up") and previous) or bool(
        extraction and extraction.is_follow_up and previous
    )
    replaced = set(target_matches and ["target"] or []) | set(origin_matches and ["origin"] or [])
    if extraction is not None:
        replaced.update(extraction.explicitly_replaced_fields)
        if extraction.origin_mentioned:
            # An explicit but unresolved Origin is a replacement attempt, not
            # permission to revive the previous session's Origin.
            replaced.add("origin")
    if follow_up and previous:
        if "target" not in replaced and target_slug is None:
            target_slug = (
                previous.get("target_slug")
                if isinstance(previous.get("target_slug"), str)
                else None
            )
            if target_slug:
                field_confidences["target"] = Decimal("1")
        if "origin" not in replaced and origin_slug is None:
            origin_slug = (
                previous.get("origin_slug")
                if isinstance(previous.get("origin_slug"), str)
                else None
            )
            if origin_slug:
                field_confidences["origin"] = Decimal("1")
    if (origin_matches and len(origin_matches) != 1) or (
        extraction is not None and extraction.origin_mentioned and origin_slug is None
    ):
        origin_slug = None
        if not notices:
            notices.append(notice_origin_unresolved())

    target_explicitly_mentioned = (
        bool(state.get("target_mentioned"))
        or bool(result.get("explicit_unknown_target"))
        or (extraction is not None and extraction.target_mentioned)
    )
    if target_slug is None and target_explicitly_mentioned:
        return {
            "response_type": "not_found",
            "error_code": "NO_TARGET",
            "resolved_intent": _intent(None, origin_slug),
            "query_mode": _query_mode(None, origin_slug),
            "notices": notices,
            "field_confidences": field_confidences,
        }
    ctx.target = next((item for item in ctx.targets if item.slug == target_slug), None)
    ctx.origin = (
        next((item for item in ctx.origins if item.slug == origin_slug), None)
        if origin_slug
        else None
    )
    if target_slug is not None and ctx.target is None:
        return {
            "response_type": "not_found",
            "error_code": "NO_TARGET",
            "resolved_intent": _intent(None, origin_slug),
            "query_mode": _query_mode(None, origin_slug),
            "notices": notices,
            "field_confidences": field_confidences,
        }
    if origin_slug and ctx.origin is None:
        origin_slug = None
        field_confidences.pop("origin", None)
        if not notices:
            notices.append(notice_origin_unresolved())
    return {
        "resolved_intent": _intent(target_slug, origin_slug),
        "field_confidences": field_confidences,
        "notices": notices,
        "origin_unresolved": bool(notices),
    }


async def _retrieve_local(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    # Re-read active catalog and the one public snapshot after Intent (or just
    # before deterministic retrieval) so an archive/status change cannot be
    # bypassed by a stale pre-LLM catalog.
    async with ctx.snapshot_factory() as session:
        ctx.targets, ctx.origins = await ctx.catalog_loader(session)
        ctx.rows = await ctx.rows_loader(session)
    intent = state.get("resolved_intent") or _intent(None, None)
    target_value = intent.get("target_slug")
    origin_value = intent.get("origin_slug")
    target_slug = target_value if isinstance(target_value, str) else None
    origin_slug = origin_value if isinstance(origin_value, str) else None
    ctx.target = next((item for item in ctx.targets if item.slug == target_slug), None)
    ctx.origin = next((item for item in ctx.origins if item.slug == origin_slug), None)
    if target_slug is not None and ctx.target is None:
        return {
            "response_type": "not_found",
            "error_code": "NO_TARGET",
            "resolved_intent": _intent(None, origin_slug),
            "query_mode": _query_mode(None, origin_slug),
            "notices": state.get("notices", []),
        }
    notices = list(state.get("notices", []))
    if origin_slug is not None and ctx.origin is None:
        origin_slug = None
        if not any(item.get("code") == "ORIGIN_UNRESOLVED" for item in notices):
            notices.append(notice_origin_unresolved())
    rows = [
        row
        for row in ctx.rows
        if row.origin.is_active
        and (target_slug is None or row.target.slug == target_slug)
        and (origin_slug is None or row.origin.slug == origin_slug)
    ]
    groups = group_rows(rows)
    mode = _query_mode(target_slug, origin_slug)
    if not groups:
        return {
            "response_type": "not_found",
            "error_code": "NO_LOCAL_RESULT",
            "resolved_intent": _intent(target_slug, origin_slug),
            "query_mode": mode,
            "notices": notices,
        }

    selected_rows = sorted(
        [choose_group(group) for group in groups.values()],
        key=route_sort_key,
    )
    if mode == "exact":
        selected = selected_rows[0]
        group = groups[(selected.target.id, selected.origin.id)]
        return {
            "response_type": "match",
            "local_lineups": [detail_from_row(selected, group)],
            "resolved_intent": _intent(target_slug, origin_slug),
            "query_mode": mode,
            "notices": notices,
        }
    return {
        "response_type": "route_matches",
        "local_lineups": [route_choice(row) for row in selected_rows],
        "resolved_intent": _intent(target_slug, origin_slug),
        "query_mode": mode,
        "notices": notices,
    }


async def _build_out_of_scope(state: AgentState, _ctx: SearchContext) -> dict[str, object]:
    return {"response_type": "out_of_scope"}


async def _build_clarification(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    return {
        "response_type": "clarification",
        "target_candidates": ctx.pending_options or state.get("target_candidates", []),
    }


async def _build_not_found(state: AgentState, _ctx: SearchContext) -> dict[str, object]:
    reason = state.get("error_code")
    return {
        "response_type": "not_found",
        "error_code": reason if reason in {"NO_TARGET", "NO_LOCAL_RESULT"} else "NO_LOCAL_RESULT",
    }


async def _build_match(state: AgentState, _ctx: SearchContext) -> dict[str, object]:
    return {"response_type": "match"}


async def _build_route_matches(state: AgentState, _ctx: SearchContext) -> dict[str, object]:
    return {"response_type": "route_matches"}


async def _persist(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    response_type = state.get("response_type")
    intent = state.get("resolved_intent") or _intent(None, None)
    if response_type == "clarification":
        await (ctx.save_conversation or save_conversation_state)(
            UUID(state["conversation_id"]),
            target_slug=None,
            origin_slug=intent.get("origin_slug")
            if isinstance(intent.get("origin_slug"), str)
            else None,
            pending_options=ctx.pending_options or state.get("target_candidates", []),
        )
    elif response_type in {"match", "route_matches", "not_found"}:
        await (ctx.save_conversation or save_conversation_state)(
            UUID(state["conversation_id"]),
            target_slug=intent.get("target_slug")
            if isinstance(intent.get("target_slug"), str)
            else None,
            origin_slug=intent.get("origin_slug")
            if isinstance(intent.get("origin_slug"), str)
            else None,
            pending_options=None,
        )
    elif response_type == "out_of_scope":
        await (ctx.save_conversation or save_conversation_state)(
            UUID(state["conversation_id"]), target_slug=None, origin_slug=None, pending_options=None
        )
    _set_event(
        ctx,
        response_type=(
            response_type
            if response_type
            in {
                "match",
                "route_matches",
                "clarification",
                "not_found",
                "out_of_scope",
            }
            else None
        ),
        target_slug=intent.get("target_slug")
        if isinstance(intent.get("target_slug"), str)
        else None,
        origin_slug=intent.get("origin_slug")
        if isinstance(intent.get("origin_slug"), str)
        else None,
    )
    return {}


def _set_event(
    ctx: SearchContext,
    *,
    response_type: str | None = None,
    target_slug: str | None = None,
    origin_slug: str | None = None,
    error_code: str | None = None,
) -> None:
    ctx.request.state.public_event = {
        "response_type": response_type,
        "target_slug": target_slug,
        "origin_slug": origin_slug,
        "error_code": error_code,
    }
    if error_code:
        ctx.request.state.error_code = error_code


def _route_after_request(state: AgentState) -> str:
    return "selection" if state["request_kind"] == "selection" else "query"


def _route_after_deterministic(state: AgentState) -> str:
    route = state.get("deterministic_result", {}).get("route")
    if route == "unsupported":
        return "unsupported"
    if len(state.get("deterministic_result", {}).get("target_matches", [])) > 1 and not state.get(
        "deterministic_result", {}
    ).get("cross_field"):
        return "ambiguous"
    return "llm" if route == "llm" else "resolve"


def _route_after_resolve(state: AgentState) -> str:
    response_type = state.get("response_type")
    if response_type == "out_of_scope":
        return "unsupported"
    if response_type == "clarification":
        return "ambiguous"
    if response_type == "not_found" and not (state.get("resolved_intent") or {}).get("target_slug"):
        return "not_found"
    return "retrieve"


def _route_after_retrieve(state: AgentState) -> str:
    response_type = state.get("response_type")
    if response_type == "match":
        return "match"
    if response_type == "route_matches":
        return "route_matches"
    return "not_found"


def _node(fn):
    async def wrapped(state, runtime=None):
        return await fn(state, _runtime_context(runtime))

    return wrapped


def _build_graph():
    builder = StateGraph(AgentState, context_schema=SearchContext)
    for name, fn in (
        ("route_request", _route_request),
        ("normalize", _normalize),
        ("deterministic_parse", _deterministic_parse),
        ("llm_parse", _llm_parse),
        ("validate_llm_output", _validate_llm_output),
        ("resolve_intent", _resolve_intent),
        ("validate_pending_target", _validate_pending_target),
        ("apply_target", _apply_target),
        ("retrieve_local", _retrieve_local),
        ("build_out_of_scope", _build_out_of_scope),
        ("build_clarification", _build_clarification),
        ("build_not_found", _build_not_found),
        ("build_match", _build_match),
        ("build_route_matches", _build_route_matches),
        ("persist_normalized_session_and_metrics", _persist),
    ):
        builder.add_node(name, _node(fn))
    builder.add_edge(START, "route_request")
    builder.add_conditional_edges(
        "route_request",
        _route_after_request,
        {"query": "normalize", "selection": "validate_pending_target"},
    )
    builder.add_edge("normalize", "deterministic_parse")
    builder.add_conditional_edges(
        "deterministic_parse",
        _route_after_deterministic,
        {
            "unsupported": "build_out_of_scope",
            "ambiguous": "build_clarification",
            "llm": "llm_parse",
            "resolve": "resolve_intent",
        },
    )
    builder.add_edge("llm_parse", "validate_llm_output")
    builder.add_edge("validate_llm_output", "resolve_intent")
    builder.add_edge("validate_pending_target", "apply_target")
    builder.add_edge("apply_target", "resolve_intent")
    builder.add_conditional_edges(
        "resolve_intent",
        _route_after_resolve,
        {
            "unsupported": "build_out_of_scope",
            "ambiguous": "build_clarification",
            "not_found": "build_not_found",
            "retrieve": "retrieve_local",
        },
    )
    builder.add_conditional_edges(
        "retrieve_local",
        _route_after_retrieve,
        {
            "match": "build_match",
            "route_matches": "build_route_matches",
            "not_found": "build_not_found",
        },
    )
    for node in (
        "build_out_of_scope",
        "build_clarification",
        "build_not_found",
        "build_match",
        "build_route_matches",
    ):
        builder.add_edge(node, "persist_normalized_session_and_metrics")
    builder.add_edge("persist_normalized_session_and_metrics", END)
    return builder.compile()


_GRAPH = _build_graph()


def _assemble_response(state: AgentState, ctx: SearchContext) -> dict[str, object]:
    conversation_id = state["conversation_id"]
    response_type = state.get("response_type")
    notices = state.get("notices", [])
    intent = state.get("resolved_intent") or _intent(None, None)
    if response_type == "out_of_scope":
        return {
            "type": "out_of_scope",
            "conversation_id": conversation_id,
            "message": "当前版本只支持 Mirage 的烟雾弹教学。",
            "supported_scope": {"maps": ["mirage"], "grenades": ["smoke"]},
        }
    if response_type == "clarification":
        options = ctx.pending_options or state.get("target_candidates", [])
        entities = {item.slug: item for item in ctx.targets}
        return {
            "type": "clarification",
            "conversation_id": conversation_id,
            "question": "你想封哪个目标位置？",
            "field": "target",
            "options": [
                {"value": slug, "label": entities[slug].name_zh}
                for slug in options
                if slug in entities
            ],
        }
    if response_type == "match":
        confidence_values = state.get("field_confidences", {})
        confidence = min(
            (Decimal(str(value)) for value in confidence_values.values()), default=Decimal("1")
        )
        return {
            "type": "match",
            "conversation_id": conversation_id,
            "source": "local_curated",
            "confidence": float(confidence),
            "intent": intent,
            "notices": notices,
            "lineup": (state.get("local_lineups") or [None])[0],
        }
    if response_type == "route_matches":
        return {
            "type": "route_matches",
            "conversation_id": conversation_id,
            "source": "local_curated",
            "intent": intent,
            "notices": notices,
            "routes": state.get("local_lineups", []),
        }
    reason = state.get("error_code") or "NO_LOCAL_RESULT"
    if reason == "NO_TARGET":
        message = "没有识别到已收录的目标位置，请换个称呼，或从首页选择目标。"
    else:
        message = "暂无符合该位置要求的站内路线，可换个起点或稍后再试。"
    return {
        "type": "not_found",
        "conversation_id": conversation_id,
        "reason": reason,
        "message": message,
    }


async def run_search(
    request: Any,
    body: Any,
    *,
    providers: ProviderSet | None = None,
    settings: Settings | None = None,
    clear_pending: Callable[[UUID], Awaitable[PendingClearResult | UUID]] | None = None,
    load_conversation: Callable[[UUID, bool], Awaitable[ConversationSnapshot | None]] | None = None,
    save_conversation: Callable[..., Awaitable[None]] | None = None,
    catalog_loader: Callable[
        [Any], Awaitable[tuple[list[CatalogEntity], list[CatalogEntity]]]
    ] = active_catalog,
    rows_loader: Callable[..., Awaitable[list[PublicRow]]] = available_rows,
    snapshot_factory: Callable[[], Any] = public_snapshot,
) -> dict[str, object]:
    """Run the only public search entrypoint under its 12-second budget."""

    active_settings = settings or get_settings()
    request.state.public_input_mode = getattr(body, "input_mode", "text")
    initial_id = body.conversation_id if getattr(body, "conversation_id", None) else uuid4()
    ctx = SearchContext(
        request=request,
        body=body,
        settings=active_settings,
        conversation_id=initial_id,
        providers=providers,
        clear_pending=clear_pending,
        load_conversation=load_conversation,
        save_conversation=save_conversation,
        catalog_loader=catalog_loader,
        rows_loader=rows_loader,
        snapshot_factory=snapshot_factory,
    )
    started = time.monotonic()
    try:
        async with asyncio.timeout(12.0):
            async with conversation_lock(initial_id, 12.0 - (time.monotonic() - started)):
                deadline = time.monotonic() + max(0.0, 12.0 - (time.monotonic() - started))
                state: AgentState = {
                    "request_id": getattr(request.state, "request_id", uuid4().hex),
                    "conversation_id": str(initial_id),
                    "request_kind": "query",
                    "raw_text": "",
                    "normalized_text": "",
                    "selected_target_slug": None,
                    "previous_intent": None,
                    "resolved_intent": None,
                    "deterministic_result": {},
                    "llm_extraction": None,
                    "field_confidences": {},
                    "target_candidates": [],
                    "target_mentioned": False,
                    "origin_mentioned": False,
                    "origin_unresolved": False,
                    "notices": [],
                    "local_lineups": [],
                    "response_type": None,
                    "error_code": None,
                    "query_mode": "catalog",
                    "deadline": deadline,
                }
                result = await _GRAPH.ainvoke(state, context=ctx)
                ctx.response = _assemble_response(result, ctx)
                return ctx.response
    except TimeoutError as exc:
        _set_event(
            ctx,
            target_slug=(ctx.target.slug if ctx.target else None),
            origin_slug=(ctx.origin.slug if ctx.origin else None),
            error_code="SEARCH_TIMEOUT",
        )
        raise AppError(504, "SEARCH_TIMEOUT", "搜索请求超时，请稍后重试。") from exc
    except ProviderFailure as exc:
        _set_event(
            ctx,
            target_slug=(ctx.target.slug if ctx.target else None),
            origin_slug=(ctx.origin.slug if ctx.origin else None),
            error_code=exc.code,
        )
        raise
    except AppError as exc:
        _set_event(
            ctx,
            target_slug=(ctx.target.slug if ctx.target else None),
            origin_slug=(ctx.origin.slug if ctx.origin else None),
            error_code=exc.code,
        )
        raise


__all__ = [
    "AgentState",
    "SearchContext",
    "candidate_decision",
    "conversation_lock",
    "load_conversation_state",
    "notice_origin_unresolved",
    "run_search",
]
