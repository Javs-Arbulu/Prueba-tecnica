"""Every mutation leaves exactly one readable trace, and the trace is accurate."""

from __future__ import annotations

import pytest

from apps.tickets import services
from apps.tickets.enums import EventType, Priority, Status
from apps.tickets.models import TicketEvent
from tests.factories import make_ticket

pytestmark = pytest.mark.django_db


def events_of(ticket):
    return list(TicketEvent.objects.filter(ticket=ticket).order_by("created_at", "id"))


def test_creation_is_recorded(agent):
    result = services.create_ticket(
        subject="Login fails",
        description="It has been failing since this morning.",
        reported_priority=Priority.HIGH,
        customer_name="Elena Duarte",
        customer_email="Elena.Duarte@Northwind.example",
        actor=agent,
    )

    (event,) = events_of(result.ticket)
    assert event.event_type == EventType.CREATED
    assert event.actor == agent
    assert event.new_value == Status.OPEN
    # The customer's address is stored normalised, so the same person is one row.
    assert result.ticket.customer.email == "elena.duarte@northwind.example"


def test_status_change_records_old_and_new(agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)

    services.change_status(
        public_id=ticket.public_id,
        actor=agent,
        new_status=Status.RESOLVED,
        note="Deployed the fix.",
    )

    (event,) = events_of(ticket)
    assert (event.event_type, event.field) == (EventType.STATUS_CHANGED, "status")
    assert (event.old_value, event.new_value) == (Status.IN_PROGRESS, Status.RESOLVED)
    assert event.note == "Deployed the fix."


def test_priority_change_records_the_reclassification(agent):
    ticket = make_ticket(Status.OPEN, priority=Priority.URGENT, reported_priority=Priority.URGENT)

    services.change_priority(
        public_id=ticket.public_id,
        actor=agent,
        new_priority=Priority.LOW,
        note="Not customer facing.",
    )

    (event,) = events_of(ticket)
    assert (event.old_value, event.new_value) == (Priority.URGENT, Priority.LOW)
    # What the customer claimed is never rewritten (ADR-09).
    ticket.refresh_from_db()
    assert ticket.reported_priority == Priority.URGENT
    assert ticket.priority == Priority.LOW


def test_assignment_snapshots_the_name_not_a_foreign_key(agent, supervisor):
    ticket = make_ticket(Status.OPEN)

    services.assign(public_id=ticket.public_id, actor=supervisor, assignee=agent)

    (event,) = events_of(ticket)
    assert event.event_type == EventType.ASSIGNED
    assert event.new_value == "Ana Reyes"

    # Renaming the agent must not rewrite history.
    agent.first_name = "Anabel"
    agent.save(update_fields=["first_name"])
    event.refresh_from_db()
    assert event.new_value == "Ana Reyes"


def test_releasing_an_in_progress_ticket_returns_it_to_the_queue(agent, supervisor):
    """ADR-14: IN_PROGRESS with nobody responsible is a lie; two events say so."""
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)

    updated = services.assign(public_id=ticket.public_id, actor=supervisor, assignee=None)

    assert updated.status == Status.OPEN
    assert updated.assignee is None
    assert [event.event_type for event in events_of(ticket)] == [
        EventType.UNASSIGNED,
        EventType.STATUS_CHANGED,
    ]


def test_a_no_op_assignment_records_nothing(agent, supervisor):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    services.assign(public_id=ticket.public_id, actor=supervisor, assignee=agent)

    assert events_of(ticket) == []
    ticket.refresh_from_db()
    assert ticket.version == 1


def test_comment_is_recorded_as_an_event_and_does_not_bump_the_version(agent):
    ticket = make_ticket(Status.OPEN)

    comment = services.add_comment(public_id=ticket.public_id, actor=agent, body="  Looking.  ")

    (event,) = events_of(ticket)
    assert event.event_type == EventType.COMMENT_ADDED
    assert event.comment == comment
    assert comment.body == "Looking."
    assert comment.is_internal is True
    ticket.refresh_from_db()
    assert ticket.version == 1  # an append-only note is not a state change


def test_editing_the_description_is_audited_too(agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    services.update_details(
        public_id=ticket.public_id,
        actor=agent,
        subject="A clearer subject",
        description="A description that is long enough.",
    )

    recorded = {(event.field, event.new_value) for event in events_of(ticket)}
    assert ("subject", "A clearer subject") in recorded
    assert ("description", "A description that is long enough.") in recorded


def test_comments_and_events_cannot_be_rewritten(agent):
    ticket = make_ticket(Status.OPEN)
    comment = services.add_comment(public_id=ticket.public_id, actor=agent, body="Original note")

    comment.body = "Something else entirely"
    with pytest.raises(ValueError, match="immutable"):
        comment.save()
    with pytest.raises(ValueError, match="immutable"):
        comment.delete()

    event = events_of(ticket)[0]
    with pytest.raises(ValueError, match="immutable"):
        event.save()


def test_every_mutation_bumps_the_version_exactly_once(agent):
    ticket = make_ticket(Status.OPEN)
    assert ticket.version == 1

    services.change_priority(public_id=ticket.public_id, actor=agent, new_priority=Priority.HIGH)
    services.assign(public_id=ticket.public_id, actor=agent, assignee=agent)
    services.change_status(public_id=ticket.public_id, actor=agent, new_status=Status.IN_PROGRESS)

    ticket.refresh_from_db()
    assert ticket.version == 4
