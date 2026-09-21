"""Rate limiting for the open door.

The per-IP limit DRF gives us is the right first line and the wrong only line:
an address is cheap and a botnet has thousands of them. This adds the dimension
infrastructure cannot see — the customer a submission claims to come from — so a
distributed flood still cannot bury one customer's queue.

It is applied from the creation service rather than as a member of
`throttle_classes`, for two reasons. A throttle runs before the body is parsed,
so keying on a field inside that body would mean reading it early, and a body
that fails to parse would surface as a confusing error instead of a plain one.
And the budget is a budget of *tickets*, not of requests: spending it in the
view would charge a customer for the retries that idempotency exists to make
free. The per-IP limit already guards the cheap path.
"""

from __future__ import annotations

import hashlib

from rest_framework.exceptions import Throttled
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.customers.models import Customer


class SubmittedEmailRateThrottle(SimpleRateThrottle):
    """Counts public submissions against the email they were sent for."""

    scope = "public_ticket_email"

    def __init__(self, email: str) -> None:
        # Normalised here rather than by the caller: `Ana@X.com` and `ana@x.com`
        # are one customer, so they have to be one bucket, and a guarantee that
        # depends on every call site remembering is not a guarantee.
        self.email = Customer.normalise_email(email)
        super().__init__()

    def get_cache_key(
        self, request: Request | None = None, view: APIView | None = None
    ) -> str | None:
        if not self.email:
            return None
        # Hashed, not raw: a cache key is a place nobody expects to find an
        # address, and the counter does not need to be readable to work.
        digest = hashlib.sha256(self.email.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


def enforce_email_rate_limit(*, email: str) -> None:
    """Spend one ticket from this address's hourly budget, or refuse.

    Neither the request nor the view is needed: the key is the address, and the
    caller is the creation service rather than a DRF hook.
    """
    throttle = SubmittedEmailRateThrottle(email)
    if not throttle.allow_request(None, None):  # type: ignore[arg-type]
        raise Throttled(throttle.wait())
