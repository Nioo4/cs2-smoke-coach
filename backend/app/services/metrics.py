"""Minimal usage-event persistence for Phase 2 business requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UsageEvent
from app.db.session import get_session_factory


def request_uuid(request_id: str) -> UUID:
    """Convert the validated 32-hex request id to the DB UUID representation."""

    return UUID(hex=request_id)


def add_admin_action(session: AsyncSession, request_id: str) -> None:
    session.add(
        UsageEvent(
            request_id=request_uuid(request_id),
            event_type="admin_action",
            input_mode="admin",
            response_type=None,
            error_code=None,
        )
    )


async def record_public_request(
    request_id: str,
    *,
    input_mode: str,
    response_type: str | None = None,
    target_slug: str | None = None,
    origin_slug: str | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> None:
    """Write one public_request in a separate short transaction.

    Public reads use a PostgreSQL read-only snapshot, so their event is
    intentionally written after that snapshot closes.  Metrics are
    operationally useful but must never turn a successful API response into a
    500 when a database is being recovered.
    """

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                session.add(
                    UsageEvent(
                        request_id=request_uuid(request_id),
                        event_type="public_request",
                        input_mode=input_mode,
                        response_type=response_type,
                        target_slug=target_slug,
                        origin_slug=origin_slug,
                        latency_ms=latency_ms,
                        error_code=error_code,
                    )
                )
    except Exception:
        # Event persistence is not a user-visible dependency.  The request
        # itself already has a stable completion/error log.
        return


def _rounded(value: object) -> int:
    if value is None:
        return 0
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return 0
        return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0


async def metrics_summary(session: AsyncSession) -> dict[str, object]:
    """Build the fixed 24-hour operations summary without a new table."""

    now = datetime.now(UTC)
    start = now - timedelta(hours=24)
    public_window = sa.and_(
        UsageEvent.event_type == "public_request",
        UsageEvent.created_at >= start,
        UsageEvent.created_at <= now,
    )
    count_result = await session.execute(
        sa.select(UsageEvent.response_type, sa.func.count())
        .where(public_window, UsageEvent.response_type.is_not(None))
        .group_by(UsageEvent.response_type)
    )
    response_counts = {
        key: 0
        for key in (
            "match",
            "route_matches",
            "clarification",
            "not_found",
            "out_of_scope",
        )
    }
    for response_type, count in count_result.all():
        metric_type = "route_matches" if response_type == "target_matches" else response_type
        if metric_type in response_counts:
            response_counts[metric_type] += int(count)

    latency_result = await session.execute(
        sa.select(
            sa.func.percentile_cont(0.5).within_group(UsageEvent.latency_ms),
            sa.func.percentile_cont(0.95).within_group(UsageEvent.latency_ms),
        ).where(public_window, UsageEvent.latency_ms.is_not(None))
    )
    p50, p95 = latency_result.one()

    provider_result = await session.execute(
        sa.select(
            UsageEvent.provider_type,
            UsageEvent.provider_outcome,
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(UsageEvent.input_tokens), 0),
            sa.func.coalesce(sa.func.sum(UsageEvent.output_tokens), 0),
            sa.func.coalesce(sa.func.sum(UsageEvent.reserved_units), 0),
        )
        .where(
            UsageEvent.event_type == "provider_reservation",
            UsageEvent.created_at >= start,
            UsageEvent.created_at <= now,
        )
        .group_by(UsageEvent.provider_type, UsageEvent.provider_outcome)
    )
    provider_usage: dict[str, dict[str, object]] = {
        "intent": {
            "attempts": 0,
            "success": 0,
            "failed": 0,
            "unknown": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "asr": {
            "attempts": 0,
            "success": 0,
            "failed": 0,
            "unknown": 0,
            "reserved_seconds": 0,
        },
    }
    for (
        provider,
        outcome,
        count,
        input_tokens,
        output_tokens,
        reserved_units,
    ) in provider_result.all():
        if provider not in provider_usage:
            continue
        item = provider_usage[provider]
        item["attempts"] = int(item["attempts"]) + int(count)
        outcome_key = "unknown" if outcome == "reserved" else outcome
        if outcome_key in {"success", "failed", "unknown"}:
            item[outcome_key] = int(item[outcome_key]) + int(count)
        if provider == "asr":
            item["reserved_seconds"] = float(item["reserved_seconds"]) + float(reserved_units)
        else:
            item["input_tokens"] = int(item["input_tokens"]) + int(input_tokens)
            item["output_tokens"] = int(item["output_tokens"]) + int(output_tokens)

    not_found_result = await session.execute(
        sa.select(
            UsageEvent.target_slug,
            UsageEvent.origin_slug,
            sa.func.count(),
        )
        .where(public_window, UsageEvent.response_type == "not_found")
        .where(UsageEvent.target_slug.is_not(None))
        .group_by(UsageEvent.target_slug, UsageEvent.origin_slug)
        .order_by(
            sa.func.count().desc(),
            UsageEvent.target_slug.asc(),
            UsageEvent.origin_slug.asc().nulls_first(),
        )
        .limit(20)
    )
    not_found = [
        {"target_slug": target, "origin_slug": origin, "count": int(count)}
        for target, origin, count in not_found_result.all()
    ]

    errors_result = await session.execute(
        sa.select(UsageEvent.error_code, sa.func.count())
        .where(public_window, UsageEvent.error_code.is_not(None))
        .group_by(UsageEvent.error_code)
        .order_by(sa.func.count().desc(), UsageEvent.error_code.asc())
    )
    errors = [{"code": code, "count": int(count)} for code, count in errors_result.all()]
    return {
        "window_hours": 24,
        "from": start,
        "to": now,
        "response_counts": response_counts,
        "latency_ms": {"p50": _rounded(p50), "p95": _rounded(p95)},
        "provider_usage": provider_usage,
        "not_found_combinations": not_found,
        "errors": errors,
    }
