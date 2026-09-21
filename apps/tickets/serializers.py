"""Serializers, one per use case.

A single serializer with conditional ``SerializerMethodField``s would be shorter
to write and far riskier to own: the day somebody adds a field, it appears in
every representation at once — including the unauthenticated one (ADR-17).
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import User
from apps.accounts.serializers import UserSerializer
from apps.customers.serializers import CustomerSerializer
from apps.tickets.enums import Priority, Status
from apps.tickets.models import Comment, Ticket, TicketEvent

NOTE_MAX_LENGTH = 2_000
#: Roughly ten pages. Generous enough for somebody pasting a stack trace, small
#: enough that an open endpoint cannot be used to push megabytes into the
#: database one request at a time. Attachments are the real answer, and they are
#: declared out of scope.
LONG_TEXT_MAX_LENGTH = 20_000


# ---------------------------------------------------------------------------
# Input: creation
# ---------------------------------------------------------------------------
class CustomerInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)


class PublicTicketCreateSerializer(serializers.Serializer):
    """What an unauthenticated customer is allowed to state.

    Note what cannot be set from here: status, assignee, and the effective
    priority. The customer reports a priority; triage decides one (ADR-09).
    """

    customer = CustomerInputSerializer()
    subject = serializers.CharField(max_length=200, min_length=3)
    description = serializers.CharField(min_length=10, max_length=LONG_TEXT_MAX_LENGTH)
    reported_priority = serializers.ChoiceField(choices=Priority.choices, default=Priority.MEDIUM)


class AgentTicketCreateSerializer(PublicTicketCreateSerializer):
    """Intake by an agent — typically a customer on the phone (ADR-10).

    The agent may record what the customer claimed *and* classify it in one go.
    """

    priority = serializers.ChoiceField(choices=Priority.choices, required=False)


# ---------------------------------------------------------------------------
# Input: actions
# ---------------------------------------------------------------------------
class NoteMixin(serializers.Serializer):
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=NOTE_MAX_LENGTH,
        help_text="Why the change was made. Stored with the audit event.",
    )


class StatusChangeSerializer(NoteMixin):
    status = serializers.ChoiceField(choices=Status.choices)


class PriorityChangeSerializer(NoteMixin):
    priority = serializers.ChoiceField(choices=Priority.choices)


class AssignSerializer(NoteMixin):
    assignee_id = serializers.UUIDField(
        allow_null=True,
        help_text="Public id of the agent, or null to release the ticket.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        raw = attrs.get("assignee_id")
        if raw is None:
            attrs["assignee"] = None
            return attrs
        try:
            attrs["assignee"] = User.objects.get(public_id=raw, is_active=True)
        except User.DoesNotExist:
            raise serializers.ValidationError(
                {"assignee_id": "No active agent matches that identifier."}
            ) from None
        return attrs


class TicketUpdateSerializer(serializers.Serializer):
    """PATCH is limited to descriptive fields; state changes have their own
    endpoints so intent never has to be inferred from a diff (ADR-16)."""

    subject = serializers.CharField(max_length=200, min_length=3, required=False)
    description = serializers.CharField(
        min_length=10, max_length=LONG_TEXT_MAX_LENGTH, required=False
    )


class CommentCreateSerializer(serializers.Serializer):
    """Comments are internal, which is all the brief asks for.

    `Comment.is_internal` stays on the model for the day a customer-facing reply
    exists, but it is not settable here: no endpoint shows a comment to a
    customer, so accepting `is_internal: false` would let an agent believe they
    had written to somebody who will never read it.
    """

    body = serializers.CharField(min_length=1, max_length=LONG_TEXT_MAX_LENGTH)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class TicketListSerializer(serializers.ModelSerializer):
    customer = CustomerSerializer(read_only=True)
    assignee = UserSerializer(read_only=True)
    comment_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Ticket
        # Annotated so the subclasses below may extend it.
        fields: tuple[str, ...] = (
            "public_id",
            "subject",
            "status",
            "priority",
            "reported_priority",
            "customer",
            "assignee",
            "comment_count",
            "version",
            "created_at",
            "updated_at",
            "last_activity_at",
        )
        read_only_fields = fields


class TicketDetailSerializer(TicketListSerializer):
    created_by = UserSerializer(read_only=True)
    allowed_transitions = serializers.SerializerMethodField()

    class Meta(TicketListSerializer.Meta):
        fields = (
            *TicketListSerializer.Meta.fields,
            "description",
            "reported_by_name",
            "created_by",
            "allowed_transitions",
            "first_assigned_at",
            "resolved_at",
            "closed_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(Status.choices)))
    def get_allowed_transitions(self, obj: Ticket) -> list[str]:
        """Published so the client renders the real state machine, not a copy."""
        return obj.allowed_transitions


class TicketPublicSerializer(serializers.ModelSerializer):
    """What a customer may see about their own ticket.

    Subject, status and dates — no description of internal work, no agent
    identity, no history, no comments (ADR-03).
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Ticket
        fields = (
            "public_id",
            "subject",
            "status",
            "status_display",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
        )
        read_only_fields = fields


class CommentSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)

    class Meta:
        model = Comment
        fields = ("public_id", "body", "is_internal", "author", "created_at")
        read_only_fields = fields


class TimelineCommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ("public_id", "body", "is_internal")
        read_only_fields = fields


class TimelineEntrySerializer(serializers.ModelSerializer):
    """One row of the merged history feed.

    Because every comment also produces a ``COMMENT_ADDED`` event, the timeline
    is a single ordered queryset rather than two lists zipped in Python — which
    is what lets it be cursor-paginated correctly (ADR-08).
    """

    id = serializers.UUIDField(source="public_id", read_only=True)
    kind = serializers.SerializerMethodField()
    type = serializers.CharField(source="event_type", read_only=True)
    actor = UserSerializer(read_only=True)
    comment = TimelineCommentSerializer(read_only=True)

    class Meta:
        model = TicketEvent
        fields = (
            "id",
            "kind",
            "type",
            "actor",
            "field",
            "old_value",
            "new_value",
            "note",
            "comment",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["comment", "event"]))
    def get_kind(self, obj: TicketEvent) -> str:
        return "comment" if obj.comment_id else "event"
