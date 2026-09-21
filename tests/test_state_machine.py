"""The state machine, exercised from every state to every state.

This is the test that has to exist: it is the only one that proves the
transition table and the code that enforces it have not drifted apart.
"""

from __future__ import annotations

import pytest

from apps.core.exceptions import InvalidTransition, TicketClosed
from apps.tickets import services
from apps.tickets.enums import (
    ALLOWED_TRANSITIONS,
    Status,
    allowed_transitions_from,
    is_transition_allowed,
)
from apps.tickets.models import TicketEvent
from tests.factories import make_ticket

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("current", Status.values)
@pytest.mark.parametrize("target", Status.values)
def test_every_pair_of_states_behaves_as_the_table_says(current, target, supervisor):
    ticket = make_ticket(current, assignee=supervisor)

    if current == Status.CLOSED:
        # A terminal ticket answers 409, not 400: the request is not malformed,
        # the resource simply no longer accepts writes.
        with pytest.raises(TicketClosed):
            services.change_status(public_id=ticket.public_id, actor=supervisor, new_status=target)
        return

    if is_transition_allowed(current, target):
        updated = services.change_status(
            public_id=ticket.public_id, actor=supervisor, new_status=target
        )
        assert updated.status == target
    else:
        with pytest.raises(InvalidTransition) as error:
            services.change_status(public_id=ticket.public_id, actor=supervisor, new_status=target)
        assert error.value.details["allowed"] == allowed_transitions_from(current)


def test_closed_is_the_only_terminal_state():
    terminal = {state for state, targets in ALLOWED_TRANSITIONS.items() if not targets}
    assert terminal == {Status.CLOSED}


def test_moving_to_in_progress_without_an_assignee_self_assigns(agent):
    ticket = make_ticket(Status.OPEN)

    updated = services.change_status(
        public_id=ticket.public_id, actor=agent, new_status=Status.IN_PROGRESS
    )

    assert updated.assignee == agent
    assert updated.first_assigned_at is not None
    # Two things happened, so two things were recorded.
    assert list(TicketEvent.objects.filter(ticket=ticket).values_list("event_type", flat=True)) == [
        "ASSIGNED",
        "STATUS_CHANGED",
    ]


def test_resolving_stamps_resolved_at_and_reopening_clears_it(agent):
    ticket = make_ticket(Status.IN_PROGRESS, assignee=agent)

    resolved = services.change_status(
        public_id=ticket.public_id, actor=agent, new_status=Status.RESOLVED
    )
    assert resolved.resolved_at is not None

    reopened = services.change_status(
        public_id=ticket.public_id, actor=agent, new_status=Status.IN_PROGRESS
    )
    assert reopened.resolved_at is None


def test_closing_stamps_closed_at(agent):
    ticket = make_ticket(Status.RESOLVED, assignee=agent)

    closed = services.change_status(
        public_id=ticket.public_id, actor=agent, new_status=Status.CLOSED
    )

    assert closed.closed_at is not None
    assert closed.allowed_transitions == []
