"""The agent-facing API: listing, filtering, actions, history, error contract."""

from __future__ import annotations

import pytest

from apps.tickets import services
from apps.tickets.enums import Priority, Status
from apps.tickets.models import Ticket
from tests.conftest import ticket_url
from tests.factories import CustomerFactory, make_ticket

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/tickets/"


def test_the_list_is_paginated_and_ordered_newest_first(agent_client):
    for _ in range(3):
        make_ticket(Status.OPEN)

    response = agent_client.get(LIST_URL)

    assert response.status_code == 200
    assert response.data["count"] == 3
    timestamps = [row["created_at"] for row in response.data["results"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_filters_combine(agent_client, agent):
    make_ticket(Status.OPEN, priority=Priority.LOW)
    wanted = make_ticket(Status.IN_PROGRESS, assignee=agent, priority=Priority.HIGH)
    make_ticket(Status.IN_PROGRESS, priority=Priority.LOW)

    response = agent_client.get(LIST_URL, {"status": Status.IN_PROGRESS, "priority": Priority.HIGH})

    assert [row["public_id"] for row in response.data["results"]] == [str(wanted.public_id)]


def test_status_accepts_several_values(agent_client):
    make_ticket(Status.OPEN)
    make_ticket(Status.RESOLVED)
    make_ticket(Status.CLOSED)

    response = agent_client.get(f"{LIST_URL}?status=OPEN&status=RESOLVED")

    assert response.data["count"] == 2


def test_the_unassigned_filter_finds_the_queue(agent_client, agent):
    make_ticket(Status.IN_PROGRESS, assignee=agent)
    make_ticket(Status.OPEN)

    response = agent_client.get(LIST_URL, {"unassigned": "true"})

    assert response.data["count"] == 1
    assert response.data["results"][0]["assignee"] is None


def test_filtering_by_assignee_and_customer_email(agent_client, agent):
    customer = CustomerFactory(name="Priya", email="priya@globex.example")
    make_ticket(Status.IN_PROGRESS, assignee=agent, customer=customer)
    make_ticket(Status.OPEN)

    by_assignee = agent_client.get(LIST_URL, {"assignee": str(agent.public_id)})
    by_email = agent_client.get(LIST_URL, {"customer_email": "PRIYA@globex.example"})

    assert by_assignee.data["count"] == 1
    assert by_email.data["count"] == 1


def test_search_looks_at_the_subject_and_the_customer(agent_client):
    make_ticket(Status.OPEN, subject="VAT rate is wrong on invoice 4412")
    make_ticket(Status.OPEN, subject="Something else")

    response = agent_client.get(LIST_URL, {"search": "vat"})

    assert response.data["count"] == 1


def test_ordering_by_priority_is_semantic_not_alphabetical(agent_client):
    make_ticket(Status.OPEN, priority=Priority.MEDIUM)
    make_ticket(Status.OPEN, priority=Priority.URGENT)
    make_ticket(Status.OPEN, priority=Priority.LOW)

    response = agent_client.get(LIST_URL, {"ordering": "-priority"})

    assert [row["priority"] for row in response.data["results"]] == [
        Priority.URGENT,
        Priority.MEDIUM,
        Priority.LOW,
    ]


def test_the_detail_publishes_the_next_legal_moves(agent_client, agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)

    response = agent_client.get(ticket_url(ticket))

    assert response.status_code == 200
    assert response.data["allowed_transitions"] == ["OPEN", "PENDING_CUSTOMER", "RESOLVED"]
    assert response.data["version"] == 1
    assert response["ETag"] == '"1"' or "ETag" not in response


def test_an_agent_creates_a_ticket_for_a_customer_on_the_phone(agent_client, agent):
    response = agent_client.post(
        LIST_URL,
        {
            "customer": {"name": "Tomás Iglesias", "email": "tomas@acme.example"},
            "subject": "Called in: cannot print the invoice",
            "description": "The customer called; the PDF download returns an empty file.",
            "reported_priority": Priority.MEDIUM,
            "priority": Priority.HIGH,
        },
    )

    assert response.status_code == 201
    ticket = Ticket.objects.get(public_id=response.data["public_id"])
    assert ticket.created_by == agent
    assert (ticket.reported_priority, ticket.priority) == (Priority.MEDIUM, Priority.HIGH)


def test_patch_only_touches_descriptive_fields(agent_client, agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    response = agent_client.patch(
        ticket_url(ticket),
        {"subject": "A much clearer subject", "status": Status.CLOSED, "version": 99},
    )

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.subject == "A much clearer subject"
    assert ticket.status == Status.OPEN
    assert ticket.version == 2


def test_tickets_cannot_be_deleted(agent_client, ticket):
    response = agent_client.delete(ticket_url(ticket))

    assert response.status_code == 405
    assert Ticket.objects.filter(pk=ticket.pk).exists()


def test_an_invalid_transition_tells_the_client_what_is_allowed(agent_client, agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    response = agent_client.post(ticket_url(ticket, "status/"), {"status": Status.PENDING_CUSTOMER})

    assert response.status_code == 400
    error = response.data["error"]
    assert error["code"] == "invalid_transition"
    assert error["details"] == {
        "current": Status.OPEN,
        "requested": Status.PENDING_CUSTOMER,
        "allowed": ["CLOSED", "IN_PROGRESS", "RESOLVED"],
    }


def test_assigning_to_an_unknown_agent_is_a_validation_error(supervisor_client, ticket):
    response = supervisor_client.post(
        ticket_url(ticket, "assign/"),
        {"assignee_id": "11111111-1111-4111-8111-111111111111"},
    )

    assert response.status_code == 400
    assert "assignee_id" in response.data["error"]["details"]["fields"]


def test_comments_are_listed_oldest_first_and_created_internal_by_default(agent_client, agent):
    ticket = make_ticket(Status.OPEN)
    agent_client.post(ticket_url(ticket, "comments/"), {"body": "First"})
    agent_client.post(ticket_url(ticket, "comments/"), {"body": "Second"})

    response = agent_client.get(ticket_url(ticket, "comments/"))

    bodies = [row["body"] for row in response.data["results"]]
    assert bodies == ["First", "Second"]
    assert all(row["is_internal"] for row in response.data["results"])
    assert response.data["results"][0]["author"]["email"] == agent.email


def test_comments_cannot_be_edited_or_deleted_through_the_api(agent_client, agent):
    ticket = make_ticket(Status.OPEN)
    agent_client.post(ticket_url(ticket, "comments/"), {"body": "Original"})

    assert (
        agent_client.patch(ticket_url(ticket, "comments/"), {"body": "Edited"}).status_code == 405
    )
    assert agent_client.delete(ticket_url(ticket, "comments/")).status_code == 405


def test_the_timeline_merges_events_and_comments_in_order(agent_client, agent, supervisor):
    ticket = make_ticket(Status.OPEN)
    services.assign(public_id=ticket.public_id, actor=supervisor, assignee=agent)
    services.change_status(public_id=ticket.public_id, actor=agent, new_status=Status.IN_PROGRESS)
    services.add_comment(public_id=ticket.public_id, actor=agent, body="Investigating now.")

    response = agent_client.get(ticket_url(ticket, "timeline/"))

    entries = response.data["results"]
    assert [entry["type"] for entry in entries] == [
        "ASSIGNED",
        "STATUS_CHANGED",
        "COMMENT_ADDED",
    ]
    assert [entry["kind"] for entry in entries] == ["event", "event", "comment"]
    assert entries[-1]["comment"]["body"] == "Investigating now."
    assert entries[0]["actor"]["email"] == supervisor.email


def test_the_timeline_is_cursor_paginated(agent_client, agent):
    ticket = make_ticket(Status.OPEN)
    for index in range(30):
        services.add_comment(public_id=ticket.public_id, actor=agent, body=f"Note {index}")

    first_page = agent_client.get(ticket_url(ticket, "timeline/"))

    assert len(first_page.data["results"]) == 25
    assert first_page.data["next"] is not None
    assert "count" not in first_page.data  # cursors do not count, by design


def test_the_agent_directory_is_available_for_the_assignment_picker(agent_client, supervisor):
    response = agent_client.get("/api/v1/agents/")

    emails = {row["email"] for row in response.data["results"]}
    assert supervisor.email in emails
    assert "password" not in response.content.decode()


def test_an_unknown_ticket_is_a_404_with_the_error_envelope(agent_client):
    response = agent_client.get("/api/v1/tickets/11111111-1111-4111-8111-111111111111/")

    assert response.status_code == 404
    assert response.data["error"]["code"] == "not_found"
