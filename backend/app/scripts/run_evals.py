"""Run the reviewed JSONL text-evaluation contract against the public API.

The runner deliberately reports only stable response fields and counts intent
reservations by request id.  It never prints the input text, prompts, audio,
response bodies, headers, or provider payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from sqlalchemy import func, select

from app.db.models import UsageEvent
from app.db.session import dispose_engine, get_session_factory

_SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_UUID = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_RESPONSE_TYPES = {"match", "route_matches", "clarification", "not_found", "out_of_scope"}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QueryTurn(_StrictModel):
    kind: Literal["query"]
    text: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    input_mode: Literal["text", "voice"]


class SelectionTurn(_StrictModel):
    kind: Literal["selection"]
    field: Literal["target"]
    value: Annotated[StrictStr, Field(pattern=_SLUG)]


Turn = Annotated[QueryTurn | SelectionTurn, Field(discriminator="kind")]
Tag = Annotated[StrictStr, Field(min_length=1, max_length=64)]


class ExpectedRoute(_StrictModel):
    target_slug: Annotated[StrictStr, Field(pattern=_SLUG)]
    origin_slug: Annotated[StrictStr, Field(pattern=_SLUG)]
    lineup_id: Annotated[StrictStr, Field(pattern=_UUID)]


class Expected(_StrictModel):
    response_type: Literal["match", "route_matches", "clarification", "not_found", "out_of_scope"]
    target_slug: Annotated[StrictStr, Field(pattern=_SLUG)] | None
    origin_slug: Annotated[StrictStr, Field(pattern=_SLUG)] | None
    selected_lineup_id: Annotated[StrictStr, Field(pattern=_UUID)] | None
    routes: list[ExpectedRoute]
    clarification_field: Literal["target"] | None
    not_found_reason: Literal["NO_TARGET", "NO_LOCAL_RESULT"] | None
    llm_calls_max: Annotated[StrictInt, Field(ge=0)]


class EvaluationCase(_StrictModel):
    id: Annotated[StrictStr, Field(pattern=r"^Q[0-9]{3}$")]
    turns: Annotated[list[Turn], Field(min_length=1, max_length=5)]
    expected: Expected
    tags: Annotated[list[Tag], Field(min_length=1, max_length=32)]


def _validate_case_semantics(case: EvaluationCase) -> None:
    expected = case.expected
    if expected.response_type == "match":
        if expected.selected_lineup_id is None or expected.routes:
            raise ValueError("match expectation shape")
    elif expected.response_type == "route_matches":
        if expected.selected_lineup_id is not None or not expected.routes:
            raise ValueError("route_matches expectation shape")
    else:
        if expected.selected_lineup_id is not None or expected.routes:
            raise ValueError("non-route expectation shape")
        if expected.target_slug is not None or expected.origin_slug is not None:
            raise ValueError("non-route intent fields")
    if expected.response_type == "not_found":
        if expected.not_found_reason is None:
            raise ValueError("not_found expectation requires reason")
    elif expected.not_found_reason is not None:
        raise ValueError("not_found reason on non-not_found")
    if expected.response_type != "clarification" and expected.clarification_field is not None:
        raise ValueError("clarification field on non-clarification")
    if expected.response_type == "clarification" and expected.clarification_field != "target":
        raise ValueError("clarification must name target")
    if not any(isinstance(turn, QueryTurn) for turn in case.turns):
        raise ValueError("case must contain a query")


def load_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"blank JSONL line {line_number}")
            try:
                value = json.loads(line)
                case = EvaluationCase.model_validate(value)
                _validate_case_semantics(case)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid evaluation case at line {line_number}") from exc
            if case.id in seen:
                raise ValueError("duplicate evaluation case id")
            seen.add(case.id)
            cases.append(case)
    if not cases:
        raise ValueError("evaluation JSONL is empty")
    return cases


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP URL")
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL contains forbidden components")
    return value.rstrip("/")


def _request_id(headers: httpx.Headers) -> str:
    value = headers.get("x-request-id", "")
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def _intent_from_response(body: dict[str, Any]) -> tuple[str | None, str | None]:
    intent = body.get("intent")
    if not isinstance(intent, dict):
        return None, None
    target = intent.get("target_slug")
    origin = intent.get("origin_slug")
    return target if isinstance(target, str) else None, origin if isinstance(origin, str) else None


def _routes_from_response(body: dict[str, Any]) -> list[tuple[str, str, str]] | None:
    routes = body.get("routes")
    if not isinstance(routes, list):
        return None
    result: list[tuple[str, str, str]] = []
    for route in routes:
        if not isinstance(route, dict):
            return None
        target = route.get("target")
        origin = route.get("origin")
        lineup = route.get("lineup")
        if (
            not isinstance(target, dict)
            or not isinstance(origin, dict)
            or not isinstance(lineup, dict)
        ):
            return None
        target_slug = target.get("slug")
        origin_slug = origin.get("slug")
        lineup_id = lineup.get("id")
        if not all(isinstance(item, str) for item in (target_slug, origin_slug, lineup_id)):
            return None
        result.append((target_slug, origin_slug, lineup_id))
    return result


def _compare(case: EvaluationCase, body: dict[str, Any]) -> str | None:
    actual_type = body.get("type")
    if actual_type not in _RESPONSE_TYPES:
        return "RESPONSE_SHAPE"
    expected = case.expected
    if actual_type != expected.response_type:
        return "RESPONSE_TYPE_MISMATCH"
    target, origin = _intent_from_response(body)
    if actual_type in {"match", "route_matches"}:
        if (target, origin) != (expected.target_slug, expected.origin_slug):
            return "INTENT_MISMATCH"
    if actual_type == "match":
        lineup = body.get("lineup")
        if not isinstance(lineup, dict) or lineup.get("id") != expected.selected_lineup_id:
            return "LINEUP_MISMATCH"
    elif actual_type == "route_matches":
        actual_routes = _routes_from_response(body)
        expected_routes = [
            (route.target_slug, route.origin_slug, route.lineup_id) for route in expected.routes
        ]
        if actual_routes != expected_routes:
            return "ROUTES_MISMATCH"
    elif actual_type == "clarification":
        if body.get("field") != expected.clarification_field:
            return "CLARIFICATION_MISMATCH"
    elif actual_type == "not_found":
        if body.get("reason") != expected.not_found_reason:
            return "NOT_FOUND_REASON_MISMATCH"
    return None


def _pace_request(last_started: float | None, interval_seconds: float) -> float:
    """Reserve the next request start slot, including across case boundaries."""

    started = time.monotonic()
    if last_started is not None:
        remaining = interval_seconds - (started - last_started)
        if remaining > 0:
            time.sleep(remaining)
            started = time.monotonic()
    return started


async def count_intent_reservations(request_ids: list[str]) -> int:
    parsed = [uuid.UUID(item) for item in request_ids if re.fullmatch(r"[0-9a-f]{32}", item)]
    if not parsed:
        return 0
    try:
        async with get_session_factory()() as session:
            result = await session.execute(
                select(func.count(UsageEvent.id)).where(
                    UsageEvent.request_id.in_(parsed),
                    UsageEvent.event_type == "provider_reservation",
                    UsageEvent.provider_type == "intent",
                )
            )
            return int(result.scalar_one())
    finally:
        await dispose_engine()


def _post_search(
    client: httpx.Client, base_url: str, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, str, str | None]:
    request_id = uuid.uuid4().hex
    try:
        response = client.post(
            f"{base_url}/api/v1/search",
            json=payload,
            headers={"X-Request-ID": request_id},
        )
    except httpx.HTTPError:
        return None, "HTTP_REQUEST_FAILED", request_id
    actual_request_id = _request_id(response.headers) or request_id
    if response.status_code != 200:
        return None, f"HTTP_{response.status_code}", actual_request_id
    try:
        body = response.json()
    except ValueError:
        return None, "INVALID_JSON", actual_request_id
    if not isinstance(body, dict):
        return None, "RESPONSE_SHAPE", actual_request_id
    return body, "", actual_request_id


def run_cases(
    cases: list[EvaluationCase], base_url: str, interval_seconds: float
) -> list[dict[str, Any]]:
    if interval_seconds < 2.2:
        raise ValueError("evaluation interval must be at least 2.2 seconds")
    results: list[dict[str, Any]] = []
    last_request_started: float | None = None
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        for case in cases:
            conversation_id: str | None = None
            request_ids: list[str] = []
            body: dict[str, Any] | None = None
            failure: str | None = None
            for turn in case.turns:
                payload: dict[str, Any]
                if isinstance(turn, QueryTurn):
                    payload = {
                        "kind": "query",
                        "text": turn.text,
                        "conversation_id": conversation_id,
                        "input_mode": turn.input_mode,
                    }
                else:
                    if conversation_id is None:
                        failure = "MISSING_CONVERSATION_ID"
                        break
                    payload = {
                        "kind": "selection",
                        "conversation_id": conversation_id,
                        "selection": {"field": turn.field, "value": turn.value},
                    }
                last_request_started = _pace_request(last_request_started, interval_seconds)
                body, request_error, request_id = _post_search(client, base_url, payload)
                if request_id:
                    request_ids.append(request_id)
                if request_error:
                    failure = request_error
                    break
                assert body is not None
                conversation_id_value = body.get("conversation_id")
                if not isinstance(conversation_id_value, str):
                    failure = "RESPONSE_SHAPE"
                    break
                conversation_id = conversation_id_value
            if failure is None and body is not None:
                failure = _compare(case, body)
            calls: int | None = None
            try:
                calls = asyncio.run(count_intent_reservations(request_ids))
            except Exception:
                if failure is None:
                    failure = "LLM_CALL_COUNT_UNAVAILABLE"
            else:
                if failure is None and calls > case.expected.llm_calls_max:
                    failure = "LLM_CALL_LIMIT_EXCEEDED"
            actual_target, actual_origin = (
                _intent_from_response(body) if isinstance(body, dict) else (None, None)
            )
            actual_lineup_id = None
            if isinstance(body, dict) and body.get("type") == "match":
                lineup = body.get("lineup")
                if isinstance(lineup, dict) and isinstance(lineup.get("id"), str):
                    actual_lineup_id = lineup["id"]
            actual_routes = _routes_from_response(body) if isinstance(body, dict) else None
            result: dict[str, Any] = {
                "id": case.id,
                "status": "PASS" if failure is None else "FAIL",
                "response_type": body.get("type") if isinstance(body, dict) else None,
                "target_slug": actual_target,
                "origin_slug": actual_origin,
                "selected_lineup_id": actual_lineup_id,
                "routes": [
                    {"target_slug": target, "origin_slug": origin, "lineup_id": lineup}
                    for target, origin, lineup in (actual_routes or [])
                ],
                "clarification_field": (
                    body.get("field")
                    if isinstance(body, dict) and body.get("type") == "clarification"
                    else None
                ),
                "not_found_reason": (
                    body.get("reason")
                    if isinstance(body, dict) and body.get("type") == "not_found"
                    else None
                ),
                "llm_calls": calls,
            }
            if failure is not None:
                result["error"] = failure
            results.append(result)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SmokeLine JSONL text evaluation")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--interval", type=float, default=2.2)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases = load_cases(args.input)
    except (OSError, ValueError):
        print("EVAL_INPUT_INVALID", file=sys.stderr)
        return 2
    if args.validate_only:
        print(f"EVAL_SCHEMA_PASS cases={len(cases)}")
        return 0
    try:
        base_url = _base_url(os.environ["PUBLIC_BASE_URL"])
        results = run_cases(cases, base_url, args.interval)
    except (KeyError, ValueError):
        print("EVAL_CONFIGURATION_INVALID", file=sys.stderr)
        return 2
    if args.output is None:
        print("EVAL_OUTPUT_REQUIRED", file=sys.stderr)
        return 2
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"cases": results}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        print("EVAL_OUTPUT_FAILED", file=sys.stderr)
        return 2
    passed = sum(result["status"] == "PASS" for result in results)
    print(f"EVAL_COMPLETE passed={passed} total={len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
