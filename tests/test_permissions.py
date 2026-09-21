"""The permission matrix, checked against the behaviour it claims to describe."""

from __future__ import annotations

import pytest

from apps.tickets.enums import Priority, Status
from tests.conftest import ticket_url
from tests.factories import make_ticket

pytestmark = pytest.mark.django_db


def test_an_agent_can_take_an_unclaimed_ticket(agent_client, agent):
    ticket = make_ticket(Status.OPEN)

    response = agent_client.post(ticket_url(ticket, "assign-to-me/"))

    assert response.status_code == 200
    assert response.data["assignee"]["email"] == agent.email


def test_an_agent_cannot_change_a_colleagues_ticket(agent_client, other_agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=other_agent)

    response = agent_client.post(ticket_url(ticket, "status/"), {"status": Status.RESOLVED})

    assert response.status_code == 403
    assert response.data["error"]["code"] == "permission_denied"


def test_an_agent_cannot_hand_a_ticket_to_somebody_else(agent_client, other_agent):
    ticket = make_ticket(Status.OPEN)

    response = agent_client.post(
        ticket_url(ticket, "assign/"), {"assignee_id": str(other_agent.public_id)}
    )

    assert response.status_code == 403
    assert "supervisor" in response.data["error"]["message"].lower()


def test_an_agent_can_release_their_own_ticket(agent_client, agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)

    response = agent_client.post(ticket_url(ticket, "assign/"), {"assignee_id": None})

    assert response.status_code == 200
    assert response.data["assignee"] is None
    assert response.data["status"] == Status.OPEN  # ADR-14


def test_an_agent_cannot_release_a_colleagues_ticket(agent_client, other_agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=other_agent)

    response = agent_client.post(ticket_url(ticket, "assign/"), {"assignee_id": None})

    assert response.status_code == 403


def test_a_supervisor_can_reassign_anything(supervisor_client, agent, other_agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)

    response = supervisor_client.post(
        ticket_url(ticket, "assign/"), {"assignee_id": str(other_agent.public_id)}
    )

    assert response.status_code == 200
    assert response.data["assignee"]["email"] == other_agent.email


def test_a_supervisor_can_change_any_ticket(supervisor_client, other_agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=other_agent)

    response = supervisor_client.post(ticket_url(ticket, "status/"), {"status": Status.RESOLVED})

    assert response.status_code == 200


def test_any_agent_may_comment_and_reprioritise(agent_client, other_agent):
    """Triage and context are everyone's job; ownership is not required for them."""
    ticket = make_ticket(Status.IN_PROGRESS, assignee=other_agent)

    comment = agent_client.post(ticket_url(ticket, "comments/"), {"body": "Saw this yesterday."})
    priority = agent_client.post(ticket_url(ticket, "priority/"), {"priority": Priority.URGENT})

    assert comment.status_code == 201
    assert priority.status_code == 200


def test_every_agent_endpoint_requires_authentication(api_client, ticket):
    responses = [
        api_client.get("/api/v1/tickets/"),
        api_client.get(ticket_url(ticket)),
        api_client.post(ticket_url(ticket, "status/"), {"status": Status.IN_PROGRESS}),
        api_client.get(ticket_url(ticket, "timeline/")),
        api_client.get("/api/v1/me/"),
        api_client.get("/api/v1/agents/"),
    ]

    assert {response.status_code for response in responses} == {401}
    assert responses[0].data["error"]["code"] == "not_authenticated"
