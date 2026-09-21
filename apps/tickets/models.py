from __future__ import annotations

import uuid
from typing import Any, NoReturn

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.tickets.enums import (
    EventType,
    Priority,
    Status,
    allowed_transitions_from,
    is_terminal,
)


class AppendOnlyModel(models.Model):
    """A row that may be written once and never rewritten.

    Enforced in the model rather than only by "we did not expose a PATCH route":
    an audit trail that any future code path can quietly edit is not an audit
    trail (ADR-12).
    """

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk is not None and not self._state.adding:
            raise ValueError(f"{type(self).__name__} rows are immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ValueError(f"{type(self).__name__} rows are immutable and cannot be deleted.")


class Ticket(models.Model):
    """A customer support request.

    Identity: ``id`` is internal and never leaves the process; the API addresses
    tickets by ``public_id`` (ADR-04), so a URL leaks neither the ticket volume
    nor a range to enumerate.
    """

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    subject = models.CharField(max_length=200)
    description = models.TextField()

    # What the customer claimed (kept forever, never overwritten) vs. what triage
    # decided (ADR-09). Keeping both is what makes "customers always say URGENT"
    # a measurable fact instead of an anecdote.
    reported_priority = models.CharField(
        max_length=16, choices=Priority.choices, default=Priority.MEDIUM, editable=False
    )
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)

    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="tickets"
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tickets",
        help_text="Null when the ticket arrived through the public endpoint.",
    )

    # Optimistic concurrency token exposed to clients as If-Match (ADR-07).
    version = models.PositiveIntegerField(default=1)
    # Fingerprint used to collapse double submits (ADR-15). NULL, not "", because
    # "this ticket was never fingerprinted" is a different fact from "its
    # fingerprint is the empty string", and only the first one is ever true here.
    dedupe_hash = models.CharField(  # noqa: DJ001
        max_length=64, null=True, blank=True, db_index=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    first_assigned_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["status", "priority"], name="ticket_status_priority_idx"),
            models.Index(fields=["assignee", "status"], name="ticket_assignee_status_idx"),
            models.Index(fields=["-created_at"], name="ticket_created_desc_idx"),
        ]
        constraints = [
            # The application already guarantees these. Stating them in the
            # database is the difference between trusting the code and
            # guaranteeing the data — including against a stray SQL update.
            models.CheckConstraint(
                condition=~Q(status=Status.RESOLVED) | Q(resolved_at__isnull=False),
                name="ticket_resolved_requires_resolved_at",
            ),
            models.CheckConstraint(
                condition=~Q(status=Status.CLOSED) | Q(closed_at__isnull=False),
                name="ticket_closed_requires_closed_at",
            ),
            models.CheckConstraint(
                condition=~Q(status=Status.IN_PROGRESS) | Q(assignee__isnull=False),
                name="ticket_in_progress_requires_assignee",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.status}] {self.subject}"

    @property
    def allowed_transitions(self) -> list[str]:
        """Handed to the client so the UI never re-implements the state machine."""
        return allowed_transitions_from(self.status)

    @property
    def is_closed(self) -> bool:
        return is_terminal(self.status)


class Comment(AppendOnlyModel):
    """An internal note on a ticket.

    ``is_internal`` defaults to ``True``: the safe default for a field that
    decides whether a customer can read something is "no".
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="comments"
    )
    body = models.TextField()
    is_internal = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=["ticket", "created_at"], name="comment_ticket_created_idx")]

    def __str__(self) -> str:
        return f"Comment by {self.author_id} on ticket {self.ticket_id}"


class TicketEvent(AppendOnlyModel):
    """One line of the ticket's history.

    ``old_value``/``new_value`` are text snapshots, not foreign keys, and that is
    deliberate: if an agent is renamed tomorrow, the history must still read the
    way it read the day it happened.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_events",
        help_text="Null when the event was produced by the public endpoint.",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    field = models.CharField(max_length=32, blank=True, default="")
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    # Set only for COMMENT_ADDED. Modelling the comment as an event lets the
    # timeline be a single ordered queryset instead of a merge done in Python,
    # which is what makes real cursor pagination possible (ADR-08).
    comment = models.OneToOneField(
        Comment, on_delete=models.CASCADE, null=True, blank=True, related_name="event"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=["ticket", "created_at"], name="event_ticket_created_idx")]

    def __str__(self) -> str:
        return f"{self.event_type} on ticket {self.ticket_id}"
