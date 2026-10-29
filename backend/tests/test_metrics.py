from __future__ import annotations

import pytest

from app.services.metrics import _rounded, metrics_summary


class _Result:
    def __init__(self, rows=(), one_value=None) -> None:
        self._rows = list(rows)
        self._one_value = one_value

    def all(self):
        return list(self._rows)

    def one(self):
        return self._one_value


class _MetricsSession:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return _Result(
                [("match", 3), ("target_matches", 2), ("route_matches", 4), ("future_response", 9)]
            )
        if self.calls == 2:
            return _Result(one_value=(None, "not-a-number"))
        if self.calls == 3:
            return _Result(
                [
                    ("intent", "success", 2, 100, 20, 0),
                    ("asr", "reserved", 1, 0, 0, 12.5),
                    ("web_search", "failed", 8, 500, 40, 0),
                    ("unsupported_provider", "success", 4, 1, 1, 1),
                ]
            )
        if self.calls == 4:
            return _Result([("vip", None, 2), ("vip", "t-spawn", 1)])
        return _Result([("DATABASE_UNAVAILABLE", 2)])


@pytest.mark.asyncio
async def test_metrics_summary_keeps_fixed_shape_for_provider_and_invalid_values() -> None:
    summary = await metrics_summary(_MetricsSession())

    assert summary["response_counts"]["match"] == 3
    assert summary["response_counts"]["route_matches"] == 6
    assert "target_matches" not in summary["response_counts"]
    assert summary["response_counts"]["clarification"] == 0
    assert summary["latency_ms"] == {"p50": 0, "p95": 0}
    assert summary["provider_usage"]["intent"] == {
        "attempts": 2,
        "success": 2,
        "failed": 0,
        "unknown": 0,
        "input_tokens": 100,
        "output_tokens": 20,
    }
    assert set(summary["provider_usage"]) == {"intent", "asr"}
    assert "web_search" not in summary["provider_usage"]
    assert summary["provider_usage"]["asr"] == {
        "attempts": 1,
        "success": 0,
        "failed": 0,
        "unknown": 1,
        "reserved_seconds": 12.5,
    }
    assert summary["not_found_combinations"] == [
        {"target_slug": "vip", "origin_slug": None, "count": 2},
        {"target_slug": "vip", "origin_slug": "t-spawn", "count": 1},
    ]
    assert summary["errors"] == [{"code": "DATABASE_UNAVAILABLE", "count": 2}]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(12.49, 12), (12.5, 13), ("99.5", 100), (None, 0), ("not-a-number", 0)],
)
def test_percentile_rounding_is_decimal_half_up(value: object, expected: int) -> None:
    assert _rounded(value) == expected
