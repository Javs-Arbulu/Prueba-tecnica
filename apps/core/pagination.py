"""Pagination strategies.

Page numbers for bounded, filterable collections; cursors for append-only feeds
that grow without a ceiling, where ``OFFSET`` degrades and concurrent inserts
shift the page boundaries under the reader.
"""

from __future__ import annotations

from rest_framework.pagination import CursorPagination, PageNumberPagination


class DefaultPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class CreatedAtCursorPagination(CursorPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    # ``id`` breaks ties: two rows written inside the same transaction can share
    # a timestamp, and a cursor needs a total order to be stable.
    ordering = ("created_at", "id")

    def get_ordering(self, request, queryset, view):
        """Pin the feed's order to the feed.

        DRF would otherwise inherit the ordering filter configured on the
        enclosing viewset — which is the right default for the ticket list and
        exactly wrong for an append-only history that must read forwards.
        """
        return self.ordering
