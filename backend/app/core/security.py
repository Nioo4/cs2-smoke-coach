"""Request identity and small security-boundary helpers."""

from __future__ import annotations

import re
import uuid

REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def normalize_request_id(value: str | None) -> str:
    """Accept only the Nginx/backend 32-hex contract; otherwise mint a UUID4 hex."""

    candidate = value or ""
    return candidate if REQUEST_ID_RE.fullmatch(candidate) else uuid.uuid4().hex
