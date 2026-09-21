"""The only place where tickets change.

Every mutation in this module follows the same three-step shape:

1. ``transaction.atomic`` + ``select_for_update``  — two agents cannot interleave
   two individually-valid transitions into one invalid pair (ADR-06).
2. Guard clauses — terminal state, optimistic version, state machine (ADR-07).
3. Mutate **and** record the event, inside that same transaction — which is what
   makes "the history cannot lie" a structural property rather than a habit.

Views never touch the ORM for writes; they authenticate, parse and delegate
(ADR-01). The cost is a little boilerplate, and the return is that the business
rules live in one readable file that can be tested without HTTP.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.accounts.models import User
from apps.core.exceptions import InvalidTransition, TicketClosed, VersionConflict
from apps.customers.models import Customer
from apps.tickets.enums import (
    STATUSES_REQUIRING_ASSIGNEE,
    EventType,
    Status,
    allowed_transitions_from,
    is_transition_allowed,
)
from apps.tickets.models import Comment, Ticket, TicketEvent

_FIELD_SEPARATOR = "\x1f"

#: How a ticket is named from outside: its public id, as a UUID or as its text
#: form. Never the internal primary key (ADR-04).
TicketRef = str | UUID


@dataclass(frozen=True)
class TicketCreation:
    """``created=False`` means a duplicate submission was collapsed (ADR-15)."""

    ticket: Ticket
    created: bool


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def create_ticket(
    *,
    subject: str,
    description: str,
    reported_priority: str,
    customer_name: str,
    customer_email: str,
    actor: User | None = None,
    priority: str | None = None,
    idempotency_key: str | None = None,
    deduplicate: bool = False,
) -> TicketCreation:
    """Create a ticket, or return the one an identical request just created.

    Shared by the public endpoint and the agent endpoint (ADR-10): the customer
    calling on the phone is the most common real intake, so both paths must
    produce exactly the same record and the same ``CREATED`` event.
    """
    fingerprint: str | None = None
    if deduplicate or idempotency_key:
        fingerprint, window = _fingerprint(
            customer_email=customer_email,
            subject=subject,
            description=description,
            idempotency_key=idempotency_key,
        )
        duplicate = _find_recent_duplicate(fingerprint, window)
        if duplicate is not None:
            return TicketCreation(ticket=duplicate, created=False)

    with transaction.atomic():
        customer = _resolve_customer(name=customer_name, email=customer_email)
        ticket = Ticket.objects.create(
            subject=subject.strip(),
            description=description.strip(),
            reported_priority=reported_priority,
            # Triage starts from what the customer reported and the agent may
            # reclassify it later; the reported value is never overwritten.
            priority=priority or reported_priority,
            status=Status.OPEN,
            customer=customer,
            created_by=actor,
            dedupe_hash=fingerprint,
        )
        _record(
            ticket,
            actor=actor,
            event_type=EventType.CREATED,
            field="status",
            new_value=Status.OPEN,
            note="Created by an agent." if actor else "Created through the public form.",
        )
        if ticket.priority != ticket.reported_priority:
            # The agent disagreed with the customer while taking the call. That
            # is a triage decision, and triage decisions are auditable.
            _record(
                ticket,
                actor=actor,
                event_type=EventType.PRIORITY_CHANGED,
                field="priority",
                old_value=ticket.reported_priority,
                new_value=ticket.priority,
                note="Classified at intake.",
            )
    return TicketCreation(ticket=ticket, created=True)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
@transaction.atomic
def change_status(
    *,
    public_id: TicketRef,
    actor: User,
    new_status: str,
    note: str = "",
    expected_version: int | None = None,
) -> Ticket:
    ticket = _lock(public_id)
    _assert_writable(ticket)
    _assert_version(ticket, expected_version)

    if not is_transition_allowed(ticket.status, new_status):
        raise InvalidTransition(
            f"Cannot move a ticket from {ticket.status} to {new_status}.",
            details={
                "current": ticket.status,
                "requested": new_status,
                "allowed": allowed_transitions_from(ticket.status),
            },
        )

    changed = ["status"]
    # A ticket in progress with nobody responsible is a lie the queue tells:
    # taking it implies owning it.
    if new_status in STATUSES_REQUIRING_ASSIGNEE and ticket.assignee_id is None:
        changed += _apply_assignment(
            ticket,
            actor=actor,
            assignee=actor,
            note="Automatically assigned when the ticket was picked up.",
        )

    previous_status = ticket.status
    ticket.status = new_status
    changed += _apply_status_timestamps(ticket, new_status)
    _save(ticket, changed)

    _record(
        ticket,
        actor=actor,
        event_type=EventType.STATUS_CHANGED,
        field="status",
        old_value=previous_status,
        new_value=new_status,
        note=note,
    )
    return ticket


@transaction.atomic
def assign(
    *,
    public_id: TicketRef,
    actor: User,
    assignee: User | None,
    note: str = "",
    expected_version: int | None = None,
) -> Ticket:
    """Assign, reassign or release a ticket.

    Releasing one that was ``IN_PROGRESS`` sends it back to ``OPEN`` (ADR-14):
    the alternative is a ticket nobody is working on that no queue shows.
    """
    ticket = _lock(public_id)
    _assert_writable(ticket)
    _assert_version(ticket, expected_version)

    if ticket.assignee_id == (assignee.id if assignee else None):
        return ticket  # nothing changed: no event, no version bump

    if assignee is not None:
        changed = _apply_assignment(ticket, actor=actor, assignee=assignee, note=note)
        _save(ticket, changed)
        return ticket

    previous = ticket.assignee
    ticket.assignee = None
    changed = ["assignee"]
    _record(
        ticket,
        actor=actor,
        event_type=EventType.UNASSIGNED,
        field="assignee",
        old_value=previous.display_name if previous else "",
        new_value="",
        note=note,
    )

    if ticket.status == Status.IN_PROGRESS:
        previous_status = ticket.status
        ticket.status = Status.OPEN
        changed.append("status")
        _record(
            ticket,
            actor=actor,
            event_type=EventType.STATUS_CHANGED,
            field="status",
            old_value=previous_status,
            new_value=Status.OPEN,
            note="Returned to the queue after losing its assignee.",
        )

    _save(ticket, changed)
    return ticket


@transaction.atomic
def change_priority(
    *,
    public_id: TicketRef,
    actor: User,
    new_priority: str,
    note: str = "",
    expected_version: int | None = None,
) -> Ticket:
    ticket = _lock(public_id)
    _assert_writable(ticket)
    _assert_version(ticket, expected_version)

    if ticket.priority == new_priority:
        return ticket

    previous = ticket.priority
    ticket.priority = new_priority
    _save(ticket, ["priority"])
    _record(
        ticket,
        actor=actor,
        event_type=EventType.PRIORITY_CHANGED,
        field="priority",
        old_value=previous,
        new_value=new_priority,
        note=note,
    )
    return ticket


@transaction.atomic
def update_details(
    *,
    public_id: TicketRef,
    actor: User,
    subject: str | None = None,
    description: str | None = None,
    expected_version: int | None = None,
) -> Ticket:
    """Edit the descriptive fields. Everything else moves through its own action."""
    ticket = _lock(public_id)
    _assert_writable(ticket)
    _assert_version(ticket, expected_version)

    changes: list[tuple[str, str, str]] = []
    if subject is not None and subject.strip() != ticket.subject:
        changes.append(("subject", ticket.subject, subject.strip()))
        ticket.subject = subject.strip()
    if description is not None and description.strip() != ticket.description:
        changes.append(("description", ticket.description, description.strip()))
        ticket.description = description.strip()

    if not changes:
        return ticket

    _save(ticket, [field for field, _, _ in changes])
    for field, old_value, new_value in changes:
        _record(
            ticket,
            actor=actor,
            event_type=EventType.DETAILS_UPDATED,
            field=field,
            old_value=old_value,
            new_value=new_value,
        )
    return ticket


@transaction.atomic
def add_comment(
    *,
    public_id: TicketRef,
    actor: User,
    body: str,
    is_internal: bool = True,
    expected_version: int | None = None,
) -> Comment:
    """Append a note to the ticket.

    The ticket's ``version`` is intentionally left alone: a comment adds to an
    append-only feed without touching the state another agent may be editing, so
    bumping it would invalidate open forms for no reason.
    """
    ticket = _lock(public_id)
    _assert_writable(ticket)
    _assert_version(ticket, expected_version)

    comment = Comment.objects.create(
        ticket=ticket,
        author=actor,
        body=body.strip(),
        is_internal=is_internal,
    )
    _record(
        ticket,
        actor=actor,
        event_type=EventType.COMMENT_ADDED,
        new_value="internal" if is_internal else "public",
        comment=comment,
    )
    return comment


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _lock(public_id: TicketRef) -> Ticket:
    """Fetch the ticket with a row lock held until the transaction commits."""
    try:
        return Ticket.objects.select_for_update().get(public_id=public_id)
    except (Ticket.DoesNotExist, ValidationError, ValueError):
        raise NotFound("No ticket matches the given identifier.") from None


def _assert_writable(ticket: Ticket) -> None:
    if ticket.is_closed:
        raise TicketClosed(
            "This ticket is closed; closed tickets are a terminal, read-only state.",
            details={"status": ticket.status, "allowed": []},
        )


def _assert_version(ticket: Ticket, expected_version: int | None) -> None:
    """Optimistic concurrency check.

    ``None`` means the client did not send ``If-Match``; simple clients keep
    working, and clients that care get last-write-wins protection.
    """
    if expected_version is None:
        return
    if ticket.version != expected_version:
        raise VersionConflict(
            "The ticket changed since you loaded it. Reload and reapply your change.",
            details={"expected_version": expected_version, "current_version": ticket.version},
        )


def _apply_assignment(
    ticket: Ticket, *, actor: User | None, assignee: User, note: str
) -> list[str]:
    previous = ticket.assignee
    ticket.assignee = assignee
    changed = ["assignee"]
    if ticket.first_assigned_at is None:
        ticket.first_assigned_at = timezone.now()
        changed.append("first_assigned_at")
    _record(
        ticket,
        actor=actor,
        event_type=EventType.ASSIGNED,
        field="assignee",
        old_value=previous.display_name if previous else "",
        new_value=assignee.display_name,
        note=note,
    )
    return changed


def _apply_status_timestamps(ticket: Ticket, new_status: str) -> list[str]:
    now = timezone.now()
    changed: list[str] = []
    if new_status == Status.RESOLVED:
        ticket.resolved_at = now
        changed.append("resolved_at")
    elif new_status == Status.CLOSED:
        ticket.closed_at = now
        changed.append("closed_at")
    elif ticket.resolved_at is not None:
        # Reopening: the ticket is not resolved any more, and pretending
        # otherwise would corrupt every resolution-time metric built on it.
        ticket.resolved_at = None
        changed.append("resolved_at")
    return changed


def _save(ticket: Ticket, changed_fields: list[str]) -> None:
    """Persist a mutation and move the optimistic-concurrency token forward."""
    ticket.version += 1
    fields = dict.fromkeys([*changed_fields, "version", "updated_at"])
    ticket.save(update_fields=list(fields))


def _record(
    ticket: Ticket,
    *,
    actor: User | None,
    event_type: str,
    field: str = "",
    old_value: str = "",
    new_value: str = "",
    note: str = "",
    comment: Comment | None = None,
) -> TicketEvent:
    return TicketEvent.objects.create(
        ticket=ticket,
        actor=actor,
        event_type=event_type,
        field=field,
        old_value=old_value or "",
        new_value=new_value or "",
        note=note or "",
        comment=comment,
    )


def _resolve_customer(*, name: str, email: str) -> Customer:
    """Customers are identified by email; a new address creates a new customer."""
    normalised = Customer.normalise_email(email)
    customer, created = Customer.objects.get_or_create(
        email=normalised, defaults={"name": name.strip()}
    )
    if not created and name.strip() and customer.name != name.strip():
        # People change how they sign their messages; keep the latest spelling.
        customer.name = name.strip()
        customer.save(update_fields=["name"])
    return customer


def _fingerprint(
    *, customer_email: str, subject: str, description: str, idempotency_key: str | None
) -> tuple[str, timedelta]:
    """Fingerprint plus the window in which a repeat counts as the same request.

    An explicit ``Idempotency-Key`` is a promise from the client, so it is
    honoured for a day. Without one, only a byte-identical submission from the
    same address within a minute is treated as a double submit — long enough to
    catch a double click or a retried request, short enough that a customer who
    genuinely writes again tomorrow gets a new ticket.
    """
    if idempotency_key:
        raw = f"key:{Customer.normalise_email(customer_email)}:{idempotency_key.strip()}"
        window = timedelta(seconds=settings.IDEMPOTENCY_KEY_WINDOW_SECONDS)
    else:
        raw = _FIELD_SEPARATOR.join(
            [
                "natural",
                Customer.normalise_email(customer_email),
                subject.strip(),
                description.strip(),
            ]
        )
        window = timedelta(seconds=settings.PUBLIC_TICKET_DEDUPE_WINDOW_SECONDS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), window


def _find_recent_duplicate(fingerprint: str, window: timedelta) -> Ticket | None:
    return (
        Ticket.objects.filter(
            dedupe_hash=fingerprint,
            created_at__gte=timezone.now() - window,
        )
        .order_by("-created_at")
        .first()
    )
