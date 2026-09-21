"""The edges: idempotent no-ops, guard clauses and the guarantees we advertise.

Each of these covers a branch that only runs when something unusual happens —
which is exactly when nobody is watching.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.test import override_settings
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.accounts.enums import Role
from apps.core.exceptions import DomainError, api_exception_handler
from apps.core.logging import JSONFormatter
from apps.customers.models import Customer
from apps.tickets import services
from apps.tickets.enums import Priority, Status
from apps.tickets.filters import SemanticOrderingFilter
from apps.tickets.models import Comment, Ticket, TicketEvent
from apps.tickets.permissions import (
    AccessContext,
    Action,
    TicketActionPermission,
    is_allowed,
)
from apps.tickets.views import parse_if_match
from tests.conftest import ticket_url
from tests.factories import UserFactory, make_ticket

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Idempotent no-ops: repeating a request must not manufacture history
# ---------------------------------------------------------------------------
def test_setting_the_priority_it_already_has_changes_nothing(agent):
    ticket = make_ticket(Status.OPEN, priority=Priority.HIGH)

    services.change_priority(public_id=ticket.public_id, actor=agent, new_priority=Priority.HIGH)

    ticket.refresh_from_db()
    assert (ticket.version, TicketEvent.objects.filter(ticket=ticket).count()) == (1, 0)


def test_patching_a_ticket_with_its_own_values_changes_nothing(agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    services.update_details(
        public_id=ticket.public_id,
        actor=agent,
        subject=ticket.subject,
        description=ticket.description,
    )

    ticket.refresh_from_db()
    assert (ticket.version, TicketEvent.objects.filter(ticket=ticket).count()) == (1, 0)


def test_an_unknown_ticket_raises_not_found_from_the_service(agent):
    with pytest.raises(NotFound):
        services.change_priority(public_id=uuid.uuid4(), actor=agent, new_priority=Priority.LOW)


def test_a_returning_customer_keeps_one_row_and_their_latest_name(agent):
    """The email is the identity; the name is how they signed this message."""
    first = services.create_ticket(
        subject="First report",
        description="Something went wrong this morning.",
        reported_priority=Priority.LOW,
        customer_name="Elena Duarte",
        customer_email="elena@northwind.example",
        actor=agent,
    )
    second = services.create_ticket(
        subject="Second report",
        description="And something else went wrong this afternoon.",
        reported_priority=Priority.LOW,
        customer_name="Elena D. Duarte",
        customer_email="ELENA@northwind.example",
        actor=agent,
    )

    assert first.ticket.customer_id == second.ticket.customer_id
    assert Customer.objects.count() == 1
    assert Customer.objects.get().name == "Elena D. Duarte"


# ---------------------------------------------------------------------------
# Permission guards
# ---------------------------------------------------------------------------
def test_an_unknown_role_is_denied_every_action():
    stranger = UserFactory.build(role="INTERN")

    assert not any(is_allowed(action, AccessContext(actor=stranger)) for action in Action)


@pytest.mark.parametrize("role", Role.values)
def test_every_known_role_can_at_least_read(role):
    user = UserFactory.build(role=role)

    assert is_allowed(Action.VIEW, AccessContext(actor=user))


def test_the_admin_cannot_be_used_to_bypass_the_service_layer(agent):
    """The audit trail is only a guarantee if there is no back door."""
    for model in (Ticket, Comment, TicketEvent):
        model_admin = admin.site._registry[model]
        assert model_admin.has_add_permission(None) is False
        assert model_admin.has_change_permission(None) is False
        assert model_admin.has_delete_permission(None) is False


# ---------------------------------------------------------------------------
# HTTP edges
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("header", ["2", '"2"', 'W/"2"', "  2  "])
def test_if_match_accepts_every_etag_spelling(agent_client, agent, header):
    ticket = make_ticket(Status.OPEN, assignee=agent)
    services.change_priority(public_id=ticket.public_id, actor=agent, new_priority=Priority.LOW)

    response = agent_client.post(
        ticket_url(ticket, "status/"), {"status": Status.IN_PROGRESS}, HTTP_IF_MATCH=header
    )

    assert response.status_code == 200


def test_no_if_match_header_means_no_expectation(rf):
    assert parse_if_match(rf.get("/")) is None


def test_ordering_without_a_parameter_leaves_the_queryset_alone():
    queryset = Ticket.objects.all()
    request = Request(APIRequestFactory().get("/"))

    filtered = SemanticOrderingFilter().filter_queryset(request, queryset, _NoOrderingView())

    assert filtered is queryset


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------
def test_a_domain_error_can_override_its_code():
    error = DomainError("Nope.", details={"why": "because"}, code="custom_rule")

    assert (error.error_code, error.details) == ("custom_rule", {"why": "because"})


def test_djangos_own_permission_denied_is_named_like_the_rest():
    response = api_exception_handler(DjangoPermissionDenied(), {})

    assert response.status_code == 403
    assert response.data["error"]["code"] == "permission_denied"


@override_settings(DEBUG=True)
def test_while_debugging_an_unhandled_exception_is_left_to_django():
    """A yellow page with a traceback beats a tidy 500 on a developer's machine."""
    assert api_exception_handler(RuntimeError("boom"), {}) is None


def test_tracebacks_reach_the_logs_as_one_json_line():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "apps.errors", logging.ERROR, __file__, 1, "failed", None, exc_info=True
        )
        record.exc_info = __import__("sys").exc_info()

    payload = JSONFormatter().format(record)

    assert "ValueError: boom" in payload
    assert payload.count("\n") == 0  # one event, one line


# ---------------------------------------------------------------------------
# Readable representations (the admin and the shell rely on them)
# ---------------------------------------------------------------------------
def test_models_describe_themselves(agent):
    ticket = make_ticket(Status.OPEN, subject="Printer on fire")
    comment = services.add_comment(public_id=ticket.public_id, actor=agent, body="Looking into it")
    event = TicketEvent.objects.get(ticket=ticket, comment=comment)

    assert str(ticket) == "[OPEN] Printer on fire"
    assert str(comment).startswith("Comment by")
    assert str(event).startswith("COMMENT_ADDED on ticket")


class _NoOrderingView:
    """A view with no ordering configured at all."""

    ordering_fields = ()
    ordering = None


# ---------------------------------------------------------------------------
# The admin: read-only, and free of the N+1 it would be easy to ship
# ---------------------------------------------------------------------------
def test_the_customer_admin_counts_tickets_without_one_query_per_row(
    customer, django_assert_num_queries
):
    for _ in range(3):
        make_ticket(Status.OPEN, customer=customer)
    model_admin = admin.site._registry[Customer]
    request = APIRequestFactory().get("/")

    with django_assert_num_queries(1):
        counts = [model_admin.ticket_count(row) for row in model_admin.get_queryset(request)]

    assert counts == [3]


def test_the_ticket_admin_loads_its_relations_up_front(agent, django_assert_num_queries):
    make_ticket(Status.IN_PROGRESS, assignee=agent)
    model_admin = admin.site._registry[Ticket]
    request = APIRequestFactory().get("/")

    with django_assert_num_queries(1):
        rows = list(model_admin.get_queryset(request))
        _ = [(row.customer.name, row.assignee.display_name) for row in rows]

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# The permission adapter refuses anything it does not recognise
# ---------------------------------------------------------------------------
def _request_from(user):
    request = Request(APIRequestFactory().get("/"))
    request.user = user
    return request


def test_the_permission_adapter_denies_what_it_does_not_recognise(agent, ticket):
    permission = TicketActionPermission()

    assert permission.has_permission(_request_from(AnonymousUser()), _View("list")) is False
    assert (
        permission.has_permission(_request_from(UserFactory.build(role="INTERN")), _View("list"))
        is False
    )
    assert permission.has_permission(_request_from(agent), _View("frobnicate")) is False
    assert (
        permission.has_object_permission(_request_from(agent), _View("frobnicate"), ticket) is False
    )


class _View:
    """The minimum a DRF permission looks at."""

    def __init__(self, action: str) -> None:
        self.action = action


# ---------------------------------------------------------------------------
# Intake by an agent
# ---------------------------------------------------------------------------
def test_classifying_at_intake_is_audited_like_any_other_reclassification(agent):
    """An agent who overrides the customer on the phone has still made a call."""
    result = services.create_ticket(
        subject="Called in: cannot print invoices",
        description="The customer says every invoice downloads as an empty file.",
        reported_priority=Priority.URGENT,
        priority=Priority.MEDIUM,
        customer_name="Tomas Iglesias",
        customer_email="tomas@acme.example",
        actor=agent,
    )

    events = list(
        TicketEvent.objects.filter(ticket=result.ticket).values_list(
            "event_type", "old_value", "new_value"
        )
    )
    assert events == [
        ("CREATED", "", Status.OPEN),
        ("PRIORITY_CHANGED", Priority.URGENT, Priority.MEDIUM),
    ]


def test_an_agent_replaying_an_idempotency_key_gets_the_same_ticket(agent_client):
    payload = {
        "customer": {"name": "Priya Raman", "email": "priya@globex.example"},
        "subject": "Called in: export never finishes",
        "description": "The quarterly export spins for two minutes and then fails.",
        "reported_priority": Priority.HIGH,
    }

    first = agent_client.post("/api/v1/tickets/", payload, HTTP_IDEMPOTENCY_KEY="call-4471")
    second = agent_client.post("/api/v1/tickets/", payload, HTTP_IDEMPOTENCY_KEY="call-4471")

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.data["public_id"] == second.data["public_id"]
    assert second["Idempotency-Replayed"] == "true"
    assert Ticket.objects.count() == 1


def test_ordering_by_status_walks_the_workflow(agent_client, agent):
    make_ticket(Status.CLOSED, assignee=agent)
    make_ticket(Status.OPEN)
    make_ticket(Status.IN_PROGRESS, assignee=agent)

    response = agent_client.get("/api/v1/tickets/", {"ordering": "status"})

    assert [row["status"] for row in response.data["results"]] == [
        Status.OPEN,
        Status.IN_PROGRESS,
        Status.CLOSED,
    ]
