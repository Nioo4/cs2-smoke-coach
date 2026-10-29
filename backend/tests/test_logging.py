import json
import logging

from app.core.logging import SafeJsonFormatter


def test_safe_formatter_is_single_line_and_omits_message_and_unknown_fields() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="SECRET_SENTINEL in exception text",
        args=(),
        exc_info=None,
    )
    record.request_id = "0123456789abcdef0123456789abcdef"
    record.path = "/healthz?secret=SECRET_SENTINEL"
    record.error_code = "INTERNAL_ERROR"
    record.untrusted = "SECRET_SENTINEL"

    line = SafeJsonFormatter().format(record)
    payload = json.loads(line)

    assert "\n" not in line
    assert "SECRET_SENTINEL" not in line
    assert set(payload) <= {
        "time",
        "level",
        "request_id",
        "http_method",
        "path",
        "status",
        "latency_ms",
        "error_code",
        "event",
        "fields",
    }
