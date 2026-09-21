"""The unauthenticated surface.

Two rules matter here: it must leak nothing, and it must survive a double click.
"""

from __future__ import annotations

import pytest
from django.test import override_settings
from rest_framework.throttling import SimpleRateThrottle

from apps.tickets import services
from apps.tickets.enums import Priority, Status
from apps.tickets.models import Ticket
from tests.factories import make_ticket

pytestmark = pytest.mark.django_db

CREATE_URL = "/api/v1/public/tickets/"

PAYLOAD = {
    "customer": {"name": "Elena Duarte", "email": "elena@northwind.example"},
    "subject": "Cannot log in after the password reset",
    "description": "The reset link says it has expired, and I have tried three times.",
    "reported_priority": Priority.HIGH,
}


def test_a_customer_can_open_a_ticket(api_client):
    response = api_client.post(CREATE_URL, PAYLOAD)

    assert response.status_code == 201
    ticket = Ticket.objects.get(public_id=response.data["public_id"])
    assert ticket.status == Status.OPEN
    assert ticket.created_by is None  # nobody internal was involved
    assert ticket.reported_priority == Priority.HIGH
    assert ticket.priority == Priority.HIGH  # triage starts from what was reported


def test_the_public_response_exposes_nothing_internal(api_client, agent):
    created = api_client.post(CREATE_URL, PAYLOAD)
    public_id = created.data["public_id"]
    services.assign(
        public_id=public_id,
        actor=agent,
        assignee=agent,
        note="Internal note that must never surface.",
    )
    services.add_comment(public_id=public_id, actor=agent, body="Internal investigation notes.")

    response = api_client.get(f"{CREATE_URL}{public_id}/")

    assert response.status_code == 200
    assert set(response.data) == {
        "public_id",
        "subject",
        "status",
        "status_display",
        "created_at",
        "updated_at",
        "resolved_at",
        "closed_at",
    }
    body = response.content.decode()
    for leak in ("Internal", agent.email, agent.first_name, "priority", "version"):
        assert leak not in body


def test_the_public_endpoints_cannot_be_used_to_mutate(api_client, ticket):
    url = f"{CREATE_URL}{ticket.public_id}/"

    assert api_client.patch(url, {"status": Status.CLOSED}).status_code == 405
    assert api_client.delete(url).status_code == 405


def test_a_customer_cannot_choose_their_own_status_or_assignee(api_client, agent):
    response = api_client.post(
        CREATE_URL,
        {**PAYLOAD, "status": Status.RESOLVED, "assignee_id": str(agent.public_id)},
    )

    ticket = Ticket.objects.get(public_id=response.data["public_id"])
    assert ticket.status == Status.OPEN
    assert ticket.assignee is None


def test_a_double_submit_within_the_window_returns_the_same_ticket(api_client):
    first = api_client.post(CREATE_URL, PAYLOAD)
    second = api_client.post(CREATE_URL, PAYLOAD)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.data["public_id"] == second.data["public_id"]
    assert second["Idempotency-Replayed"] == "true"
    assert Ticket.objects.count() == 1


@override_settings(PUBLIC_TICKET_DEDUPE_WINDOW_SECONDS=0)
def test_outside_the_window_an_identical_request_is_a_new_ticket(api_client):
    api_client.post(CREATE_URL, PAYLOAD)
    api_client.post(CREATE_URL, PAYLOAD)

    assert Ticket.objects.count() == 2


def test_an_idempotency_key_collapses_requests_that_are_not_identical(api_client):
    first = api_client.post(CREATE_URL, PAYLOAD, HTTP_IDEMPOTENCY_KEY="abc-123")
    second = api_client.post(
        CREATE_URL,
        {**PAYLOAD, "subject": "Different subject entirely"},
        HTTP_IDEMPOTENCY_KEY="abc-123",
    )

    assert first.data["public_id"] == second.data["public_id"]
    assert Ticket.objects.count() == 1


def test_an_idempotency_key_is_scoped_to_the_customer(api_client):
    api_client.post(CREATE_URL, PAYLOAD, HTTP_IDEMPOTENCY_KEY="abc-123")
    api_client.post(
        CREATE_URL,
        {**PAYLOAD, "customer": {"name": "Someone Else", "email": "other@client.example"}},
        HTTP_IDEMPOTENCY_KEY="abc-123",
    )

    assert Ticket.objects.count() == 2


@pytest.mark.parametrize(
    "payload",
    [
        {**PAYLOAD, "subject": ""},
        {**PAYLOAD, "description": "short"},
        {**PAYLOAD, "customer": {"name": "X", "email": "not-an-email"}},
        {**PAYLOAD, "reported_priority": "CATASTROPHIC"},
    ],
)
def test_invalid_submissions_are_rejected_with_the_error_envelope(api_client, payload):
    response = api_client.post(CREATE_URL, payload)

    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"
    assert response.data["error"]["details"]["fields"]


def test_public_creation_is_throttled_per_client(api_client, monkeypatch):
    """The open door needs a limit, or it is a free ticket generator.

    The rate is patched on the throttle class rather than through the settings:
    DRF binds ``THROTTLE_RATES`` once at import time, so overriding the setting
    would or would not take effect depending on module import order.
    """
    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {
            "public_ticket_create": "2/min",
            "public_ticket_read": "1000/hour",
            "public_ticket_email": "1000/hour",
        },
    )

    for index in range(2):
        payload = {**PAYLOAD, "subject": f"Distinct subject number {index}"}
        assert api_client.post(CREATE_URL, payload).status_code == 201

    blocked = api_client.post(CREATE_URL, {**PAYLOAD, "subject": "Yet another subject"})

    assert blocked.status_code == 429
    assert blocked.data["error"]["code"] == "throttled"
    assert blocked.data["error"]["details"]["retry_after_seconds"] is not None


def test_an_unknown_public_id_is_a_plain_404(api_client):
    response = api_client.get(f"{CREATE_URL}11111111-1111-4111-8111-111111111111/")

    assert response.status_code == 404
    assert response.data["error"]["code"] == "not_found"


def test_the_public_view_of_a_resolved_ticket_shows_the_dates(api_client, agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)
    services.change_status(public_id=ticket.public_id, actor=agent, new_status=Status.RESOLVED)

    response = api_client.get(f"{CREATE_URL}{ticket.public_id}/")

    assert response.data["status"] == Status.RESOLVED
    assert response.data["status_display"] == "Resolved"
    assert response.data["resolved_at"] is not None
