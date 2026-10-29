#!/usr/bin/env python3
"""Warm and measure deterministic map/search reads in an isolated stack.

This is an internal throughput check, not a public-SLA probe.  It uses only
the Python standard library so the backend image can run it without a new
dependency.  Response bodies are inspected for the stable response type only;
they are never printed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Sample:
    endpoint: str
    elapsed_ms: float
    error: str | None


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("no latency samples")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _url(base_url: str, endpoint: str) -> str:
    return base_url.rstrip("/") + endpoint


class _PersistentHTTPClient:
    def __init__(self, base_url: str) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid performance base URL")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._prefix = parsed.path.rstrip("/")
        self._connection: http.client.HTTPConnection | None = None

    def _open(self) -> http.client.HTTPConnection:
        connection_type = (
            http.client.HTTPSConnection
            if self._scheme == "https"
            else http.client.HTTPConnection
        )
        self._connection = connection_type(self._host, self._port, timeout=15)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def request(
        self,
        method: str,
        endpoint: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        connection = self._connection or self._open()
        target = f"{self._prefix}{endpoint}"
        try:
            connection.request(
                method,
                target,
                body=body,
                headers={**headers, "Connection": "keep-alive"},
            )
            response = connection.getresponse()
            response_body = response.read()
            status = response.status
            if response.will_close:
                self.close()
            return status, response_body
        except (OSError, TimeoutError, http.client.HTTPException):
            self.close()
            raise


def _request(
    base_url: str, endpoint: str, client: _PersistentHTTPClient | None = None
) -> Sample:
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    owned_client = client is None
    if endpoint == "/api/v1/search":
        payload = json.dumps(
            {
                "kind": "query",
                "text": "我在 T 出生点，想封 VIP 烟",
                "conversation_id": None,
                "input_mode": "text",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        method = "POST"
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Request-ID": request_id,
        }
    else:
        method = "GET"
        payload = None
        headers = {"X-Request-ID": request_id}
    error: str | None = None
    try:
        if client is None:
            client = _PersistentHTTPClient(base_url)
        status, body = client.request(method, endpoint, payload, headers)
        if status != 200:
            error = f"HTTP_{status}"
        elif endpoint.endswith("/search"):
            try:
                decoded = json.loads(body)
            except (TypeError, ValueError):
                error = "INVALID_JSON"
            else:
                if not isinstance(decoded, dict) or decoded.get("type") != "match":
                    error = "UNEXPECTED_SEARCH_RESPONSE"
    except (OSError, TimeoutError, http.client.HTTPException, ValueError):
        error = "REQUEST_FAILED"
    finally:
        if owned_client and client is not None:
            client.close()
    return Sample(endpoint, (time.perf_counter() - started) * 1000, error)


def _run_mixed(base_url: str, total: int, concurrency: int) -> list[Sample]:
    if total < 1 or concurrency < 1:
        raise ValueError("invalid request count or concurrency")
    endpoints = ("/api/v1/maps/mirage", "/api/v1/search")
    jobs = [endpoints[index % 2] for index in range(total)]
    pending: queue.Queue[tuple[int, str] | None] = queue.Queue()
    samples: list[Sample | None] = [None] * total

    def worker() -> None:
        client: _PersistentHTTPClient | None = None
        try:
            try:
                client = _PersistentHTTPClient(base_url)
            except (OSError, ValueError):
                client = None
            while True:
                job = pending.get()
                try:
                    if job is None:
                        return
                    index, endpoint = job
                    if client is None:
                        samples[index] = Sample(endpoint, 0.0, "REQUEST_FAILED")
                    else:
                        samples[index] = _request(base_url, endpoint, client)
                except (OSError, TimeoutError, http.client.HTTPException, ValueError):
                    if job is not None:
                        samples[job[0]] = Sample(job[1], 0.0, "REQUEST_FAILED")
                finally:
                    pending.task_done()
        finally:
            if client is not None:
                client.close()

    workers = [
        threading.Thread(target=worker, name=f"smokeline-load-{index}")
        for index in range(concurrency)
    ]
    for thread in workers:
        thread.start()
    for index, endpoint in enumerate(jobs):
        pending.put((index, endpoint))
    for _ in workers:
        pending.put(None)
    pending.join()
    for thread in workers:
        thread.join()
    if any(sample is None for sample in samples):
        raise RuntimeError("PERF_WORKER_FAILED")
    return [sample for sample in samples if sample is not None]


def _warmup(base_url: str, count: int, concurrency: int) -> None:
    if count != 20 or concurrency != 20:
        raise ValueError("Phase 5C warmup requires 20 mixed requests at concurrency 20")
    samples = _run_mixed(base_url, count, concurrency)
    if any(sample.error is not None for sample in samples):
        raise RuntimeError("PERF_WARMUP_FAILED")


def _readyz(base_url: str) -> None:
    request = urllib.request.Request(_url(base_url, "/api/v1/readyz"), method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("PERF_READYZ_FAILED")
            response.read()
    except (OSError, urllib.error.HTTPError) as exc:
        raise RuntimeError("PERF_READYZ_FAILED") from exc


def _assert_fixture_count(base_url: str, expected: int) -> None:
    request = urllib.request.Request(
        _url(base_url, "/api/v1/maps/mirage"), method="GET"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("PERF_FIXTURES_FAILED")
            body = json.loads(response.read())
    except (OSError, urllib.error.HTTPError, TypeError, ValueError) as exc:
        raise RuntimeError("PERF_FIXTURES_FAILED") from exc
    if (
        not isinstance(body, dict)
        or int(body.get("published_lineup_count", 0)) < expected
    ):
        raise RuntimeError("PERF_FIXTURES_FAILED")


def _assert_database_fixtures(expected: int) -> None:
    """Confirm the test-only seed rows are published/default/ready in PG."""

    try:
        import psycopg

        with (
            psycopg.connect(
                host=os.environ.get("POSTGRES_HOST", "postgres"),
                port=5432,
                dbname="smokeline",
                user="smokeline",
                password=os.environ.get("POSTGRES_PASSWORD", "smokeline"),
                connect_timeout=10,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                    SELECT count(DISTINCT l.id)
                    FROM lineups AS l
                    JOIN targets AS t ON t.id = l.target_id
                    JOIN tutorial_videos AS v ON v.lineup_id = l.id
                    WHERE t.slug LIKE 'phase5c-perf-target-%'
                      AND l.status = 'published'
                      AND l.is_default = true
                      AND v.status = 'ready'
                    """
            )
            row = cursor.fetchone()
        if not row or int(row[0]) != expected:
            raise RuntimeError("PERF_FIXTURES_FAILED")
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("PERF_FIXTURES_FAILED") from exc


def _postgres_connection_count() -> int:
    try:
        import psycopg

        with (
            psycopg.connect(
                host=os.environ.get("POSTGRES_HOST", "postgres"),
                port=5432,
                dbname="smokeline",
                user="smokeline",
                password=os.environ.get("POSTGRES_PASSWORD", "smokeline"),
                connect_timeout=10,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database()"
            )
            value = cursor.fetchone()
        if not value:
            raise RuntimeError("PERF_POOL_CHECK_FAILED")
        return int(value[0])
    except Exception as exc:
        raise RuntimeError("PERF_POOL_CHECK_FAILED") from exc


def _intent_reservation_count() -> int:
    try:
        import psycopg

        with (
            psycopg.connect(
                host=os.environ.get("POSTGRES_HOST", "postgres"),
                port=5432,
                dbname="smokeline",
                user="smokeline",
                password=os.environ.get("POSTGRES_PASSWORD", "smokeline"),
                connect_timeout=10,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT count(*) FROM usage_events "
                "WHERE event_type = 'provider_reservation' AND provider_type = 'intent'"
            )
            row = cursor.fetchone()
        if not row:
            raise RuntimeError("PERF_AI_RESERVATION_CHECK_FAILED")
        return int(row[0])
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("PERF_AI_RESERVATION_CHECK_FAILED") from exc


def _assert_connection_bound(connections: int) -> None:
    try:
        pool_size = int(os.environ.get("DB_POOL_SIZE", "10"))
        max_overflow = int(os.environ.get("DB_MAX_OVERFLOW", "0"))
    except ValueError as exc:
        raise RuntimeError("PERF_POOL_CHECK_FAILED") from exc
    # The probe itself contributes one pg_stat_activity row; the application
    # must remain within its configured SQLAlchemy pool plus that probe.
    if connections > pool_size + max_overflow + 1:
        raise RuntimeError("PERF_POOL_BOUND_EXCEEDED")


def run_load(base_url: str, total: int, concurrency: int, warmup: int) -> list[Sample]:
    if total < 2 or total % 2:
        raise ValueError("total must be an even positive number")
    if concurrency < 1 or warmup < 0:
        raise ValueError("invalid concurrency or warmup")
    if warmup:
        _warmup(base_url, warmup, concurrency)
    return _run_mixed(base_url, total, concurrency)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SmokeLine isolated deterministic read load"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKELINE_LOAD_BASE_URL", "http://backend:8000"),
    )
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--max-search-p95-ms", type=float, default=500.0)
    parser.add_argument("--max-map-p95-ms", type=float, default=300.0)
    parser.add_argument("--expected-fixtures", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _readyz(args.base_url)
        _assert_fixture_count(args.base_url, args.expected_fixtures)
        _assert_database_fixtures(args.expected_fixtures)
        _warmup(args.base_url, args.warmup, args.concurrency)
        baseline_connections = _postgres_connection_count()
        _assert_connection_bound(baseline_connections)
        baseline_intent_reservations = _intent_reservation_count()
        samples = run_load(args.base_url, args.requests, args.concurrency, 0)
        _readyz(args.base_url)
        final_connections = _postgres_connection_count()
        _assert_connection_bound(final_connections)
        final_intent_reservations = _intent_reservation_count()
        if final_connections != baseline_connections:
            raise RuntimeError("PERF_POOL_LEAK")
        if final_intent_reservations != baseline_intent_reservations:
            raise RuntimeError("PERF_AI_RESERVATION")
        grouped: dict[str, list[Sample]] = {}
        for sample in samples:
            grouped.setdefault(sample.endpoint, []).append(sample)
        errors = sum(sample.error is not None for sample in samples)
        intent_reservation_delta = (
            final_intent_reservations - baseline_intent_reservations
        )
        overall_p95 = percentile([sample.elapsed_ms for sample in samples], 0.95)
        search_p95 = percentile(
            [sample.elapsed_ms for sample in grouped["/api/v1/search"]], 0.95
        )
        map_p95 = percentile(
            [sample.elapsed_ms for sample in grouped["/api/v1/maps/mirage"]], 0.95
        )
        print(
            f"PERF fixtures={args.expected_fixtures} samples={len(samples)} errors={errors} "
            f"overall_p95_ms={overall_p95:.2f} search_p95_ms={search_p95:.2f} "
            f"map_p95_ms={map_p95:.2f} pg_connections={final_connections} "
            f"intent_reservations_before={baseline_intent_reservations} "
            f"intent_reservations_after={final_intent_reservations} "
            f"intent_reservations_delta={intent_reservation_delta}"
        )
        if (
            errors
            or overall_p95 > args.max_search_p95_ms
            or search_p95 > args.max_search_p95_ms
            or map_p95 > args.max_map_p95_ms
        ):
            return 1
        print("PERF_PASS")
        return 0
    except (OSError, RuntimeError, ValueError, KeyError):
        print("PERF_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
