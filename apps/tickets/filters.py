"""Query-string filtering for the agent ticket list."""

from __future__ import annotations

import django_filters
from django.db.models import QuerySet
from rest_framework.filters import OrderingFilter

from apps.tickets.enums import Priority, Status
from apps.tickets.models import Ticket


class TicketFilter(django_filters.FilterSet):
    """The filters an agent's working day actually needs.

    ``status`` and ``priority`` accept repeated values (``?status=OPEN&status=
    IN_PROGRESS``) because "my open work" is rarely a single status.
    """

    status = django_filters.MultipleChoiceFilter(choices=Status.choices)
    priority = django_filters.MultipleChoiceFilter(choices=Priority.choices)
    assignee = django_filters.UUIDFilter(field_name="assignee__public_id")
    unassigned = django_filters.BooleanFilter(
        field_name="assignee", lookup_expr="isnull", label="Only unclaimed tickets"
    )
    customer_email = django_filters.CharFilter(field_name="customer__email", lookup_expr="iexact")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Ticket
        fields = [
            "status",
            "priority",
            "assignee",
            "unassigned",
            "customer_email",
            "created_after",
            "created_before",
        ]


class SemanticOrderingFilter(OrderingFilter):
    """Sort by meaning, not by alphabet.

    ``?ordering=-priority`` should put URGENT first, and ``?ordering=status``
    should walk the workflow. Sorting the raw columns puts ``URGENT`` after
    ``MEDIUM`` and ``CLOSED`` before ``OPEN``, which is technically correct and
    operationally useless.
    """

    field_aliases = {"priority": "priority_rank", "status": "status_rank"}

    def filter_queryset(self, request, queryset: QuerySet, view) -> QuerySet:
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        translated = [
            ("-" if term.startswith("-") else "")
            + self.field_aliases.get(term.lstrip("-"), term.lstrip("-"))
            for term in ordering
        ]
        return queryset.order_by(*translated)
