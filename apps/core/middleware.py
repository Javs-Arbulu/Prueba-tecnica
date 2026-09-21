"""Request-scoped correlation id.

Every log line, every error payload and every response header carry the same
id, which is what turns "it failed for a customer at 3pm" into something a
developer can actually grep for.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar

from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("apps.request")

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

#: A client may propagate its own id (useful when the API sits behind a gateway),
#: but only if it looks like an id. Anything else is replaced rather than logged.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

REQUEST_ID_HEADER = "X-Request-ID"


def get_request_id() -> str:
    """Return the id of the request being served, or ``"-"`` outside a request."""
    return _request_id_var.get()


class RequestIDMiddleware:
    """Assign an id to each request and log a structured access line."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = self._resolve_request_id(request)
        request.request_id = request_id  # type: ignore[attr-defined]
        token = _request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = self.get_response(request)
        finally:
            _request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response[REQUEST_ID_HEADER] = request_id
        # The path only: a query string here can carry a customer's email or the
        # text an agent searched for, and logs are the wrong place for either.
        logger.info(
            "%s %s %s",
            request.method,
            request.path,
            response.status_code,
            extra={
                "http_method": request.method,
                "path": request.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "request_id": request_id,
            },
        )
        return response

    @staticmethod
    def _resolve_request_id(request: HttpRequest) -> str:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        if incoming and _SAFE_REQUEST_ID.match(incoming):
            return incoming
        return uuid.uuid4().hex
