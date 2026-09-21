"""The open door.

`POST /public/tickets/` is the only endpoint an attacker reaches without a
credential, so every control on it earns a test that tries to get past it.
"""

from __future__ import annotations

import json

import pytest
from django.test import override_settings
from rest_framework.throttling import SimpleRateThrottle

from apps.core.checks import shared_cache_is_configured
from apps.customers.models import Customer
from apps.tickets import services
from apps.tickets.models import Ticket
from apps.tickets.serializers import LONG_TEXT_MAX_LENGTH
from tests.conftest import ticket_url

pytestmark = pytest.mark.django_db

CREATE_URL = "/api/v1/public/tickets/"


def payload(**overrides) -> dict:
    base = {
        "customer": {"name": "Elena Duarte", "email": "elena@northwind.example"},
        "subject": "Cannot log in after the password reset",
        "description": "The reset link says it has expired and I have tried three times.",
        "reported_priority": "HIGH",
    }
    base.update(overrides)
    return base


def rates(**overrides) -> dict:
    base = {
        "public_ticket_create": "1000/hour",
        "public_ticket_email": "1000/hour",
        "public_ticket_read": "1000/hour",
        "auth_token": "1000/min",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Identity: an open endpoint must not be able to rewrite somebody else's name
# ---------------------------------------------------------------------------
def test_a_public_submission_cannot_rename_an_existing_customer(api_client, customer):
    original = customer.name

    response = api_client.post(
        CREATE_URL,
        payload(customer={"name": "HIJACKED NAME", "email": customer.email}),
    )

    assert response.status_code == 201
    customer.refresh_from_db()
    assert customer.name == original


def test_the_submitted_name_is_kept_on_the_ticket_instead(api_client, customer):
    """Not discarded: a name that differs from the customer record is a signal an
    agent wants to see, whether it is a colleague reporting or an impersonation."""
    response = api_client.post(
        CREATE_URL,
        payload(customer={"name": "Someone Else", "email": customer.email}),
    )

    ticket = Ticket.objects.get(public_id=response.data["public_id"])
    assert ticket.reported_by_name == "Someone Else"
    assert ticket.customer.name != "Someone Else"


def test_an_agent_may_still_correct_a_customers_name(agent):
    """The authenticated path is audited by `created_by`, so it keeps the
    convenience the public one loses."""
    Customer.objects.create(name="Elena Duarte", email="elena@northwind.example")

    services.create_ticket(
        subject="Called in with a correction",
        description="The customer spells their name differently.",
        reported_priority="LOW",
        customer_name="Elena Duarte-Ruiz",
        customer_email="elena@northwind.example",
        actor=agent,
    )

    assert Customer.objects.get(email="elena@northwind.example").name == "Elena Duarte-Ruiz"


def test_the_public_view_still_exposes_nothing_internal(api_client, customer):
    created = api_client.post(CREATE_URL, payload(customer={"name": "X", "email": customer.email}))

    response = api_client.get(f"{CREATE_URL}{created.data['public_id']}/")

    assert "reported_by_name" not in response.data
    assert "X" not in response.content.decode()


def test_an_agent_sees_the_reported_name_on_the_detail(agent_client, api_client, customer):
    created = api_client.post(
        CREATE_URL, payload(customer={"name": "Reported By", "email": customer.email})
    )
    ticket = Ticket.objects.get(public_id=created.data["public_id"])

    response = agent_client.get(ticket_url(ticket))

    assert response.data["reported_by_name"] == "Reported By"


# ---------------------------------------------------------------------------
# Size: text that nobody bounded is storage an open endpoint can fill
# ---------------------------------------------------------------------------
def test_a_description_beyond_the_limit_is_refused(api_client):
    response = api_client.post(CREATE_URL, payload(description="A" * (LONG_TEXT_MAX_LENGTH + 1)))

    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"
    assert "description" in response.data["error"]["details"]["fields"]


def test_a_description_at_the_limit_is_accepted(api_client):
    response = api_client.post(CREATE_URL, payload(description="A" * LONG_TEXT_MAX_LENGTH))

    assert response.status_code == 201


def test_a_comment_beyond_the_limit_is_refused(agent_client, ticket):
    response = agent_client.post(
        ticket_url(ticket, "comments/"), {"body": "A" * (LONG_TEXT_MAX_LENGTH + 1)}
    )

    assert response.status_code == 400


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=2_000)
def test_a_body_too_large_for_django_still_answers_the_error_contract(api_client):
    """Django rejects this before DRF exists. Without our handler it would be the
    one response in the whole API that a client cannot parse."""
    response = api_client.post(CREATE_URL, payload(description="A" * 5_000))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


# ---------------------------------------------------------------------------
# Surface: parsers and renderers the endpoint does not need
# ---------------------------------------------------------------------------
def test_the_public_endpoint_only_speaks_json(api_client):
    """Multipart is the file-upload parser, and this endpoint never takes a file."""
    response = api_client.post(
        CREATE_URL,
        {"subject": "Sent as a form", "description": "Trying the multipart parser."},
        format="multipart",
    )

    assert response.status_code == 415
    assert response.data["error"]["code"] == "unsupported_media_type"


def test_an_unmatched_url_answers_json_not_html(api_client):
    response = api_client.get("/this/route/does/not/exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Rate limiting: two dimensions, because the edge only sees one
# ---------------------------------------------------------------------------
def test_one_email_is_limited_even_from_many_addresses(api_client, monkeypatch):
    """A botnet has thousands of IPs. It does not have thousands of customers."""
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates(public_ticket_email="2/hour"))

    statuses = [
        api_client.post(
            CREATE_URL, payload(subject=f"Distinct subject {index}"), REMOTE_ADDR=f"10.0.0.{index}"
        ).status_code
        for index in range(3)
    ]

    assert statuses == [201, 201, 429]


def test_many_emails_from_one_address_are_limited_by_ip(api_client, monkeypatch):
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates(public_ticket_create="2/hour"))

    statuses = [
        api_client.post(
            CREATE_URL,
            payload(customer={"name": "X", "email": f"person{index}@client.example"}),
        ).status_code
        for index in range(3)
    ]

    assert statuses == [201, 201, 429]


def test_the_email_limit_ignores_case_and_spacing(api_client, monkeypatch):
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates(public_ticket_email="1/hour"))

    first = api_client.post(CREATE_URL, payload(customer={"name": "X", "email": "ana@x.example"}))
    second = api_client.post(
        CREATE_URL,
        payload(subject="Another subject", customer={"name": "X", "email": " ANA@X.example "}),
    )

    assert (first.status_code, second.status_code) == (201, 429)


@pytest.mark.parametrize(
    "broken",
    [
        {"subject": "No customer at all", "description": "Ten characters or more here."},
        {"customer": "not-an-object", "subject": "Wrong shape", "description": "Long enough."},
        {"customer": {"name": "X"}, "subject": "No email", "description": "Long enough here."},
    ],
)
def test_a_submission_without_a_usable_email_fails_validation_not_the_throttle(api_client, broken):
    """The throttle has nothing to key on; it must step aside, not explode."""
    response = api_client.post(CREATE_URL, broken)

    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"


def test_an_unparseable_body_does_not_break_the_throttle(api_client):
    response = api_client.post(CREATE_URL, data="{not json", content_type="application/json")

    assert response.status_code == 400
    assert response.data["error"]["code"] == "malformed_request"


def test_x_forwarded_for_cannot_be_used_to_pick_a_bucket(api_client, monkeypatch):
    """The regression this locks in: DRF's own default trusts X-Forwarded-For
    whenever it is present, so anyone could invent a value and get a fresh
    bucket per request. NUM_PROXIES=0 pins identity to REMOTE_ADDR."""
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates(public_ticket_create="1/hour"))

    first = api_client.post(CREATE_URL, payload(), HTTP_X_FORWARDED_FOR="10.0.0.1")
    second = api_client.post(
        CREATE_URL, payload(subject="Another subject"), HTTP_X_FORWARDED_FOR="10.0.0.2"
    )

    assert (first.status_code, second.status_code) == (201, 429)


# ---------------------------------------------------------------------------
# The control that fails quietly
# ---------------------------------------------------------------------------
def test_a_per_process_cache_is_allowed_where_it_is_harmless():
    assert shared_cache_is_configured(None) == []


@override_settings(REQUIRE_SHARED_CACHE=True)
def test_a_per_process_cache_stops_a_production_boot():
    """Throttle counters live in the cache: a local one multiplies every limit by
    the number of workers, and says nothing."""
    errors = shared_cache_is_configured(None)

    assert [error.id for error in errors] == ["core.E001"]
    assert "worker" in errors[0].hint


@override_settings(
    REQUIRE_SHARED_CACHE=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache"}},
)
def test_a_shared_cache_passes_the_check():
    assert shared_cache_is_configured(None) == []


@override_settings(
    REST_FRAMEWORK={
        **__import__("django.conf", fromlist=["settings"]).settings.REST_FRAMEWORK,
        "NUM_PROXIES": 1,
    }
)
def test_behind_one_proxy_the_limit_follows_the_real_client(api_client, monkeypatch):
    """The other half of the knob: once a proxy is declared, the header it sets
    is what identifies the client, instead of every request sharing the balancer."""
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates(public_ticket_create="1/hour"))

    first = api_client.post(CREATE_URL, payload(), HTTP_X_FORWARDED_FOR="10.0.0.1")
    other_client = api_client.post(
        CREATE_URL, payload(subject="Another subject"), HTTP_X_FORWARDED_FOR="10.0.0.2"
    )
    same_client = api_client.post(
        CREATE_URL, payload(subject="A third subject"), HTTP_X_FORWARDED_FOR="10.0.0.1"
    )

    assert (first.status_code, other_client.status_code, same_client.status_code) == (201, 201, 429)


# ---------------------------------------------------------------------------
# The handlers Django reaches for when DRF is not involved
# ---------------------------------------------------------------------------
def test_djangos_own_handlers_speak_the_same_contract():
    from django.core.exceptions import DisallowedHost, PermissionDenied, RequestDataTooBig
    from django.test import RequestFactory

    from apps.core.views import bad_request, permission_denied, server_error

    request = RequestFactory().get("/")

    suspicious = bad_request(request, DisallowedHost("bad host"))
    # The same condition on a URL DRF never sees, such as the admin.
    oversized = bad_request(request, RequestDataTooBig())
    forbidden = permission_denied(request, PermissionDenied())
    crashed = server_error(request)

    assert (suspicious.status_code, forbidden.status_code, crashed.status_code) == (400, 403, 500)
    assert oversized.status_code == 413
    assert json.loads(oversized.content)["error"]["code"] == "request_too_large"
    assert json.loads(suspicious.content)["error"]["code"] == "malformed_request"
    assert json.loads(forbidden.content)["error"]["code"] == "permission_denied"
    assert json.loads(crashed.content)["error"]["code"] == "server_error"


def test_without_an_email_there_is_nothing_to_count_against():
    """An unnamed submitter cannot be rate limited by name; validation answers."""
    from rest_framework.test import APIRequestFactory

    from apps.tickets.throttling import SubmittedEmailRateThrottle, enforce_email_rate_limit

    assert SubmittedEmailRateThrottle("").get_cache_key(None, None) is None
    enforce_email_rate_limit(APIRequestFactory().post("/"), None, email="   ")
