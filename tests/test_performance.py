"""N+1 guards.

The point of these tests is not the exact number. It is that the number does not
grow with the number of rows — the failure mode that only shows up in production,
with real volume, long after the code was reviewed.
"""

from __future__ import annotations

import pytest

from apps.tickets import services
from apps.tickets.enums import Status
from tests.conftest import ticket_url
from tests.factories import UserFactory, make_ticket

pytestmark = pytest.mark.django_db


def _populate(count: int) -> None:
    for _ in range(count):
        agent = UserFactory()
        ticket = make_ticket(Status.OPEN, assignee=agent)
        services.add_comment(public_id=ticket.public_id, actor=agent, body="A note.")


def test_the_ticket_list_costs_the_same_for_one_row_or_twenty(
    agent_client, django_assert_num_queries
):
    _populate(1)
    with django_assert_num_queries(2) as first:
        agent_client.get("/api/v1/tickets/")

    _populate(19)
    with django_assert_num_queries(len(first)):
        response = agent_client.get("/api/v1/tickets/")

    assert response.data["count"] == 20


def test_the_timeline_costs_the_same_whatever_its_length(
    agent_client, agent, django_assert_num_queries
):
    ticket = make_ticket(Status.OPEN)
    services.add_comment(public_id=ticket.public_id, actor=agent, body="First note.")

    with django_assert_num_queries(2) as first:
        agent_client.get(ticket_url(ticket, "timeline/"))

    for index in range(15):
        services.add_comment(public_id=ticket.public_id, actor=agent, body=f"Note {index}")

    with django_assert_num_queries(len(first)):
        response = agent_client.get(ticket_url(ticket, "timeline/"))

    assert len(response.data["results"]) == 16


def test_the_comment_list_does_not_query_once_per_author(agent_client, django_assert_num_queries):
    ticket = make_ticket(Status.OPEN)
    for _ in range(10):
        services.add_comment(public_id=ticket.public_id, actor=UserFactory(), body="A note.")

    with django_assert_num_queries(2):
        response = agent_client.get(ticket_url(ticket, "comments/"))

    assert len(response.data["results"]) == 10
