"""The invariants, enforced by the database rather than by good intentions.

Each of these tries to write an inconsistent row with raw ``UPDATE``s, bypassing
every model, serializer and service. If the application were the only thing
holding the invariant, they would all pass.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.tickets.enums import Status
from apps.tickets.models import Ticket
from tests.factories import make_ticket

pytestmark = pytest.mark.django_db


def test_resolved_without_a_resolution_timestamp_is_impossible(agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    with pytest.raises(IntegrityError), transaction.atomic():
        Ticket.objects.filter(pk=ticket.pk).update(status=Status.RESOLVED, resolved_at=None)


def test_closed_without_a_closing_timestamp_is_impossible(agent):
    ticket = make_ticket(Status.OPEN, assignee=agent)

    with pytest.raises(IntegrityError), transaction.atomic():
        Ticket.objects.filter(pk=ticket.pk).update(status=Status.CLOSED, closed_at=None)


def test_in_progress_without_an_assignee_is_impossible():
    ticket = make_ticket(Status.OPEN)

    with pytest.raises(IntegrityError), transaction.atomic():
        Ticket.objects.filter(pk=ticket.pk).update(status=Status.IN_PROGRESS, assignee=None)


def test_a_customer_with_open_tickets_cannot_be_deleted(ticket):
    """PROTECT: losing the reporter would orphan the incident."""
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        ticket.customer.delete()


def test_deactivating_an_agent_leaves_their_tickets_standing(agent):
    """SET_NULL: staff turnover must not cascade into deleted history."""
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)
    Ticket.objects.filter(pk=ticket.pk).update(status=Status.OPEN)

    agent.delete()

    ticket.refresh_from_db()
    assert ticket.assignee is None
