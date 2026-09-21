"""The vocabulary of the domain, and the state machine that governs it.

The transition table is *data*. A chain of ``if`` statements spread across views
would say the same thing, but it could not be enumerated, tested exhaustively,
or handed to the client so the UI stops guessing which buttons to draw.
"""

from __future__ import annotations

from django.db import models


class Status(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    # Waiting on the customer, who answers by email or phone: there is no
    # customer portal (ADR-03/ADR-11). Only an agent moves a ticket out of here.
    PENDING_CUSTOMER = "PENDING_CUSTOMER", "Pending customer"
    RESOLVED = "RESOLVED", "Resolved"
    CLOSED = "CLOSED", "Closed"


class Priority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


class EventType(models.TextChoices):
    """What happened, in the language of the business.

    Not "column X went from a to b" — an agent reading the history wants events,
    not a row diff (ADR-05).
    """

    CREATED = "CREATED", "Created"
    STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
    ASSIGNED = "ASSIGNED", "Assigned"
    UNASSIGNED = "UNASSIGNED", "Unassigned"
    PRIORITY_CHANGED = "PRIORITY_CHANGED", "Priority changed"
    COMMENT_ADDED = "COMMENT_ADDED", "Comment added"
    # Not in the original plan: the Definition of Done requires that no change
    # goes unrecorded, and subject/description are editable.
    DETAILS_UPDATED = "DETAILS_UPDATED", "Details updated"


#: The whole state machine. Anything not listed here is impossible, by definition.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    Status.OPEN: frozenset({Status.IN_PROGRESS, Status.RESOLVED, Status.CLOSED}),
    Status.IN_PROGRESS: frozenset({Status.PENDING_CUSTOMER, Status.RESOLVED, Status.OPEN}),
    Status.PENDING_CUSTOMER: frozenset({Status.IN_PROGRESS, Status.RESOLVED, Status.OPEN}),
    Status.RESOLVED: frozenset({Status.CLOSED, Status.IN_PROGRESS}),  # the second one is a reopen
    Status.CLOSED: frozenset(),  # terminal, on purpose (ADR-13)
}

#: Statuses that require somebody to be responsible for the ticket.
STATUSES_REQUIRING_ASSIGNEE: frozenset[str] = frozenset({Status.IN_PROGRESS})

#: Statuses that mean "an agent is on the hook for this". Releasing a ticket in
#: one of them sends it back to the queue, because the alternative is a ticket
#: nobody is working on that no queue shows. `PENDING_CUSTOMER` belongs here for
#: the same reason `IN_PROGRESS` does: when the customer finally replies, that
#: reply has to land on somebody.
STATUSES_RETURNED_TO_QUEUE_ON_RELEASE: frozenset[str] = frozenset(
    {Status.IN_PROGRESS, Status.PENDING_CUSTOMER}
)

#: Order used when sorting by "how urgent is this", instead of alphabetically.
PRIORITY_RANK: dict[str, int] = {
    Priority.LOW: 0,
    Priority.MEDIUM: 1,
    Priority.HIGH: 2,
    Priority.URGENT: 3,
}

#: Order used when sorting by "how far along is this". Alphabetically, a closed
#: ticket would come first and an open one third, which is nobody's mental model.
STATUS_RANK: dict[str, int] = {
    Status.OPEN: 0,
    Status.IN_PROGRESS: 1,
    Status.PENDING_CUSTOMER: 2,
    Status.RESOLVED: 3,
    Status.CLOSED: 4,
}


def allowed_transitions_from(status: str) -> list[str]:
    """Destinations reachable from ``status``, sorted for a stable API payload."""
    return sorted(ALLOWED_TRANSITIONS.get(status, frozenset()))


def is_transition_allowed(current: str, new: str) -> bool:
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal(status: str) -> bool:
    return not ALLOWED_TRANSITIONS.get(status, frozenset())
