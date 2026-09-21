"""Two agents, one ticket.

The pessimistic lock keeps the write path serialised; the optimistic version
keeps a stale form from silently undoing somebody else's work (ADR-06/ADR-07).
"""

from __future__ import annotations

import pytest

from apps.core.exceptions import VersionConflict
from apps.tickets import services
from apps.tickets.enums import Priority, Status
from tests.conftest import ticket_url
from tests.factories import make_ticket

pytestmark = pytest.mark.django_db


def test_a_stale_if_match_is_rejected(agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)
    stale_version = ticket.version

    services.change_priority(
        public_id=ticket.public_id,
        actor=agent,
        new_priority=Priority.HIGH,
        expected_version=stale_version,
    )

    with pytest.raises(VersionConflict):
        services.change_status(
            public_id=ticket.public_id,
            actor=agent,
            new_status=Status.IN_PROGRESS,
            expected_version=stale_version,
        )


def test_two_writes_with_the_same_if_match_header_return_409(agent_client, agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)
    headers = {"HTTP_IF_MATCH": str(ticket.version)}

    first = agent_client.post(
        ticket_url(ticket, "status/"), {"status": Status.IN_PROGRESS}, **headers
    )
    second = agent_client.post(
        ticket_url(ticket, "status/"), {"status": Status.RESOLVED}, **headers
    )

    assert first.status_code == 200
    assert first["ETag"] == '"2"'
    assert second.status_code == 409
    assert second.data["error"]["code"] == "version_conflict"
    assert second.data["error"]["details"] == {"expected_version": 1, "current_version": 2}


def test_without_if_match_a_simple_client_still_works(agent_client, agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    response = agent_client.post(ticket_url(ticket, "status/"), {"status": Status.IN_PROGRESS})

    assert response.status_code == 200


def test_a_malformed_if_match_is_a_client_error(agent_client, agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    response = agent_client.post(
        ticket_url(ticket, "status/"),
        {"status": Status.IN_PROGRESS},
        HTTP_IF_MATCH="not-a-number",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"


def test_an_etag_round_trip_is_accepted(agent_client, agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)
    detail = agent_client.get(ticket_url(ticket))

    response = agent_client.post(
        ticket_url(ticket, "status/"),
        {"status": Status.IN_PROGRESS},
        HTTP_IF_MATCH=detail["ETag"],
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [
        ("status/", {"status": Status.IN_PROGRESS}),
        ("priority/", {"priority": Priority.LOW}),
        ("assign/", {"assignee_id": None}),
        ("comments/", {"body": "One more thought"}),
    ],
)
def test_a_closed_ticket_rejects_every_write_with_409(agent_client, agent, suffix, payload):
    ticket = make_ticket(Status.CLOSED, assignee=agent)

    response = agent_client.post(ticket_url(ticket, suffix), payload)

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ticket_closed"


def test_patching_a_closed_ticket_is_also_refused(agent_client, agent):
    ticket = make_ticket(Status.CLOSED, assignee=agent)

    response = agent_client.patch(ticket_url(ticket), {"subject": "A new subject"})

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ticket_closed"


def test_the_write_path_takes_a_row_lock(agent):
    """A regression guard: the lock is what makes the audit trail trustworthy."""
    from django.db import connection

    ticket = make_ticket(Status.OPEN, assignee=agent)
    statements: list[str] = []

    def capture(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        services.change_priority(
            public_id=ticket.public_id, actor=agent, new_priority=Priority.HIGH
        )

    assert any("FOR UPDATE" in statement for statement in statements)
