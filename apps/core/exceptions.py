"""Domain exceptions and the single place where errors become HTTP payloads.

Every failure — validation, permission, domain rule, crash — leaves the API in
the same shape:

    {"error": {"code": ..., "message": ..., "details": {...}, "request_id": ...}}

A client can branch on ``code`` without parsing prose, and ``request_id`` ties
the response to the server logs.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.core.middleware import get_request_id

logger = logging.getLogger("apps.errors")


class DomainError(APIException):
    """Base class for rule violations raised by the service layer."""

    # Annotated so subclasses can widen it; without it the literal 400 becomes
    # the declared type and every 409 below is a type error.
    status_code: int = status.HTTP_400_BAD_REQUEST
    error_code = "domain_error"
    default_detail = "The operation violates a domain rule."

    def __init__(
        self,
        detail: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        self.details: dict[str, Any] = details or {}
        if code:
            self.error_code = code
        super().__init__(detail or self.default_detail)


class InvalidTransition(DomainError):
    """The requested status is not reachable from the current one."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "invalid_transition"
    default_detail = "That status transition is not allowed."


class VersionConflict(DomainError):
    """The client's ``If-Match`` version is stale (ADR-07)."""

    status_code = status.HTTP_409_CONFLICT
    error_code = "version_conflict"
    default_detail = "The ticket was modified by someone else. Reload and retry."


class TicketClosed(DomainError):
    """A write was attempted on a terminal ticket."""

    status_code = status.HTTP_409_CONFLICT
    error_code = "ticket_closed"
    default_detail = "This ticket is closed and cannot be modified."


class InvalidAssignment(DomainError):
    """The assignment target is not a valid internal agent."""

    error_code = "invalid_assignment"
    default_detail = "The assignment target is not a valid agent."


#: DRF speaks its own vocabulary of codes; this is the translation into ours.
_DRF_CODE_MAP = {
    "invalid": "validation_error",
    "parse_error": "validation_error",
    "authentication_failed": "not_authenticated",
    "not_authenticated": "not_authenticated",
    "permission_denied": "permission_denied",
    "not_found": "not_found",
    "method_not_allowed": "method_not_allowed",
    "unsupported_media_type": "unsupported_media_type",
    "throttled": "throttled",
}

#: Validation failures never echo a crafted message back; the offending fields
#: go in `details` instead.
_VALIDATION_MESSAGE = "The request payload is invalid."


def build_error_payload(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": get_request_id(),
        }
    }


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    response = drf_exception_handler(exc, context)

    if response is None:
        # Nothing DRF recognises: a genuine bug. Keep the contract for clients,
        # but let the debugger through while developing.
        logger.exception("Unhandled exception", extra={"view": str(context.get("view"))})
        if settings.DEBUG:
            return None
        return Response(
            build_error_payload(
                code="server_error",
                message="An unexpected error occurred.",
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    code, message, details = _classify(exc, response)
    response.data = build_error_payload(code=code, message=message, details=details)
    return response


def _classify(exc: Exception, response: Response) -> tuple[str, str, dict[str, Any]]:
    if isinstance(exc, DomainError):
        return exc.error_code, str(exc.detail), exc.details

    # DRF translates these two into its own exceptions internally but hands us
    # the original, so they need naming here or they fall through as "error".
    if isinstance(exc, Http404):
        return "not_found", "No resource matches the given identifier.", {}
    if isinstance(exc, DjangoPermissionDenied):
        return "permission_denied", "You do not have permission to perform this action.", {}

    drf_code = getattr(exc, "default_code", None) or "error"
    code = _DRF_CODE_MAP.get(drf_code, drf_code)
    detail = getattr(exc, "detail", None)

    if code == "validation_error":
        return code, _VALIDATION_MESSAGE, {"fields": _normalise_validation(detail)}

    if code == "throttled":
        wait = getattr(exc, "wait", None)
        return code, str(detail), {"retry_after_seconds": int(wait) if wait else None}

    return code, str(detail), {}


def _normalise_validation(detail: Any) -> Any:
    """Turn DRF's nested ``ErrorDetail`` structures into plain JSON types."""
    if isinstance(detail, dict):
        return {key: _normalise_validation(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return [_normalise_validation(item) for item in detail]
    return str(detail)
