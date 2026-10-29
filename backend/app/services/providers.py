"""Small provider protocols, test doubles, and process-safe daily quotas."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app.core.config import Settings, get_settings
from app.core.errors import AppError, database_is_unavailable
from app.db.models import UsageEvent
from app.db.session import get_session_factory
from app.schemas.agent import IntentExtraction
from app.services.metrics import request_uuid


class IntentParser(Protocol):
    async def parse(
        self,
        text: str,
        catalog: object,
        previous_intent: dict[str, object] | None,
    ) -> IntentExtraction: ...


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    usage_duration: float


class SpeechTranscriber(Protocol):
    async def transcribe(self, audio_path: Path, hotwords: dict[str, int]) -> Transcription: ...


class ProviderFailure(AppError):
    """An upstream failure with a retry classification for the graph."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.retryable = retryable
        super().__init__(status_code, code, message)


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    id: UUID
    request_id: UUID
    provider_type: str
    reserved_units: Decimal


ReservationFn = Callable[[str, Decimal], Awaitable[QuotaReservation]]
OutcomeFn = Callable[..., Awaitable[None]]

_QUOTA_LOCKS: dict[str, asyncio.Lock] = {}
_QUOTA_GUARD = asyncio.Lock()
_SHANGHAI = ZoneInfo("Asia/Shanghai")


async def _quota_lock(provider_type: str) -> asyncio.Lock:
    async with _QUOTA_GUARD:
        return _QUOTA_LOCKS.setdefault(provider_type, asyncio.Lock())


def shanghai_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the current Asia/Shanghai civil day as aware UTC bounds."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local = current.astimezone(_SHANGHAI)
    start_local = datetime.combine(local.date(), time.min, tzinfo=_SHANGHAI)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _quota_limit(settings: Settings, provider_type: str) -> int:
    if provider_type == "intent":
        return settings.daily_llm_call_limit
    if provider_type == "asr":
        return settings.daily_asr_seconds_limit
    raise ValueError(f"unsupported quota provider: {provider_type}")


def _quota_error(provider_type: str) -> AppError:
    code = "DAILY_ASR_QUOTA_EXHAUSTED" if provider_type == "asr" else "DAILY_LLM_QUOTA_EXHAUSTED"
    return AppError(503, code, "今日的受控服务额度已用尽，请稍后再试。")


async def reserve_provider_call(
    request_id: str | UUID,
    input_mode: str,
    provider_type: str,
    *,
    units: Decimal | int | float = Decimal("1"),
    settings: Settings | None = None,
    now: datetime | None = None,
) -> QuotaReservation:
    """Atomically aggregate reservations and insert one new reservation row."""

    active_settings = settings or get_settings()
    amount = Decimal(str(units))
    if amount <= 0:
        raise ValueError("reservation units must be positive")
    if isinstance(request_id, UUID):
        request_uuid_value = request_id
    else:
        try:
            request_uuid_value = request_uuid(str(request_id))
        except ValueError:
            request_uuid_value = UUID(str(request_id))
    lock = await _quota_lock(provider_type)
    async with lock:
        # Resolve the civil day only after this provider's process lock is
        # acquired. A caller queued just before Shanghai midnight may not
        # enter the transaction until the next day; using the pre-wait day
        # would then check one window while inserting into another.
        effective_now = now or datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        else:
            effective_now = effective_now.astimezone(UTC)
        start, end = shanghai_day_bounds(effective_now)
        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    total = await session.scalar(
                        sa.select(
                            sa.func.coalesce(sa.func.sum(UsageEvent.reserved_units), 0)
                        ).where(
                            UsageEvent.event_type == "provider_reservation",
                            UsageEvent.provider_type == provider_type,
                            UsageEvent.created_at >= start,
                            UsageEvent.created_at < end,
                        )
                    )
                    if Decimal(str(total or 0)) + amount > _quota_limit(
                        active_settings, provider_type
                    ):
                        raise _quota_error(provider_type)
                    row = UsageEvent(
                        id=uuid4(),
                        request_id=request_uuid_value,
                        event_type="provider_reservation",
                        input_mode=input_mode,
                        provider_type=provider_type,
                        reserved_units=amount,
                        provider_outcome="reserved",
                        created_at=effective_now,
                    )
                    session.add(row)
                    await session.flush()
                    return QuotaReservation(row.id, request_uuid_value, provider_type, amount)
        except AppError:
            raise
        except Exception as exc:
            if isinstance(
                exc, (sa.exc.DBAPIError, sa.exc.TimeoutError)
            ) and database_is_unavailable(exc):
                raise AppError(
                    503, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试。"
                ) from None
            raise


async def finish_provider_call(
    reservation: QuotaReservation,
    outcome: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_code: str | None = None,
) -> None:
    """Update a reservation in its own short transaction."""

    if outcome not in {"success", "failed"}:
        raise ValueError("provider outcome must be success or failed")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (input_tokens, output_tokens)
    ):
        raise ValueError("provider token counts must be non-negative integers")
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                row = await session.get(UsageEvent, reservation.id, with_for_update=True)
                if row is not None and row.provider_outcome == "reserved":
                    row.provider_outcome = outcome
                    row.input_tokens = input_tokens
                    row.output_tokens = output_tokens
                    row.error_code = error_code
    except Exception:
        # A reservation deliberately remains reserved when the outcome cannot
        # be persisted; that is the Spec's conservative unknown accounting.
        return


class DisabledIntentParser:
    async def parse(self, _text, _catalog, _previous_intent) -> IntentExtraction:
        raise AppError(503, "INTENT_AI_UNAVAILABLE", "意图解析服务暂不可用，请使用明确的目标名称。")


class FakeIntentParser:
    async def parse(self, text, _catalog, _previous_intent) -> IntentExtraction:
        normalized = str(text).strip().lower()
        if normalized == "从匪家封中路看人的窗口":
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
        return IntentExtraction(
            map=None,
            grenade=None,
            target_candidates=[],
            origin_candidates=[],
            target_mentioned=False,
            origin_mentioned=False,
            is_follow_up=False,
            explicitly_replaced_fields=[],
        )


class DisabledSpeechTranscriber:
    async def transcribe(self, _audio_path: Path, _hotwords: dict[str, int]) -> Transcription:
        raise AppError(503, "ASR_UNAVAILABLE", "语音识别服务暂不可用，请改用文字输入。")


class FakeSpeechTranscriber:
    def __init__(self, duration_seconds: float) -> None:
        self.duration_seconds = max(0.0, float(duration_seconds))

    async def transcribe(self, _audio_path: Path, _hotwords: dict[str, int]) -> Transcription:
        return Transcription("我在 Mirage 的 T 出生点，想封 VIP 烟", self.duration_seconds)


@dataclass(frozen=True, slots=True)
class ProviderSet:
    intent: IntentParser


def provider_factory(
    *,
    settings: Settings | None = None,
    request_id: str | None = None,
    input_mode: str = "text",
    deadline: float | None = None,
    reservation_fn: ReservationFn | None = None,
    outcome_fn: OutcomeFn | None = None,
) -> ProviderSet:
    """Construct the single Intent provider for a request."""

    active = settings or get_settings()
    if active.ai_provider == "disabled":
        return ProviderSet(DisabledIntentParser())
    if active.ai_provider == "fake":
        active.validate_runtime()
        return ProviderSet(FakeIntentParser())
    if active.ai_provider != "dashscope":
        raise AppError(503, "INTENT_AI_UNAVAILABLE", "意图解析服务暂不可用，请使用明确的目标名称。")
    from app.services.dashscope_provider import DashScopeIntentParser

    common = {
        "settings": active,
        "request_id": request_id,
        "input_mode": input_mode,
        "deadline": deadline,
        "reservation_fn": reservation_fn
        or bind_reservation(request_id or "00000000000000000000000000000000", input_mode, active),
        "outcome_fn": outcome_fn or finish_provider_call,
    }
    return ProviderSet(DashScopeIntentParser(**common))


def speech_transcriber_factory(
    *,
    settings: Settings | None = None,
    request_id: str | None = None,
    input_mode: str = "voice",
    deadline: float | None = None,
    duration_seconds: float = 0.0,
    reservation_fn: ReservationFn | None = None,
    outcome_fn: OutcomeFn | None = None,
    transport: object | None = None,
) -> SpeechTranscriber:
    """Construct the request-scoped speech provider without doing I/O."""

    active = settings or get_settings()
    if active.ai_provider == "disabled":
        return DisabledSpeechTranscriber()
    if active.ai_provider == "fake":
        active.validate_runtime()
        return FakeSpeechTranscriber(duration_seconds)
    if active.ai_provider != "dashscope":
        raise AppError(503, "ASR_UNAVAILABLE", "语音识别服务暂不可用，请改用文字输入。")
    from app.services.dashscope_provider import DashScopeSpeechTranscriber

    common = {
        "settings": active,
        "request_id": request_id,
        "input_mode": input_mode,
        "deadline": deadline,
        "duration_seconds": duration_seconds,
        "reservation_fn": reservation_fn
        or bind_reservation(request_id or "00000000000000000000000000000000", input_mode, active),
        "outcome_fn": outcome_fn or finish_provider_call,
        "transport": transport,
    }
    return DashScopeSpeechTranscriber(**common)


def bind_reservation(
    request_id: str,
    input_mode: str,
    settings: Settings | None = None,
) -> ReservationFn:
    async def reserve(provider_type: str, units: Decimal) -> QuotaReservation:
        return await reserve_provider_call(
            request_id,
            input_mode,
            provider_type,
            units=units,
            settings=settings,
        )

    return reserve
