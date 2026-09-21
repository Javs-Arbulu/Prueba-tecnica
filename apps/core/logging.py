"""Structured logging.

One JSON object per line: greppable by a human, ingestable by a log shipper,
and always stamped with the request id produced by ``RequestIDMiddleware``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from apps.core.middleware import get_request_id

#: Attributes ``logging`` puts on every record; anything else the caller passed
#: through ``extra=`` is forwarded into the JSON payload as-is.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
    "request_id",
}


class RequestIDFilter(logging.Filter):
    """Make ``%(request_id)s`` usable by any formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", get_request_id()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
