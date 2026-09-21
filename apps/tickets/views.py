"""HTTP layer.

Views do three things and no more: authenticate, parse, delegate. Anything that
looks like a business rule in here would be a bug waiting to be duplicated in
the next endpoint (ADR-01).
"""

from __future__ import annotations

from typing import cast

from django.db.models import Case, Count, IntegerField, QuerySet, Value, When
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import GenericAPIView, RetrieveAPIView
from rest_framework.pagination import BasePagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.models import User
from apps.core.pagination import CreatedAtCursorPagination
from apps.tickets import services
from apps.tickets.enums import PRIORITY_RANK, STATUS_RANK
from apps.tickets.filters import SemanticOrderingFilter, TicketFilter
from apps.tickets.models import Comment, Ticket, TicketEvent
from apps.tickets.permissions import TicketActionPermission
from apps.tickets.serializers import (
    AgentTicketCreateSerializer,
    AssignSerializer,
    CommentCreateSerializer,
    CommentSerializer,
    PriorityChangeSerializer,
    PublicTicketCreateSerializer,
    StatusChangeSerializer,
    TicketDetailSerializer,
    TicketListSerializer,
    TicketPublicSerializer,
    TicketUpdateSerializer,
    TimelineEntrySerializer,
)

UUID_URL_REGEX = "[0-9a-fA-F-]{36}"

IF_MATCH_PARAMETER = OpenApiParameter(
    name="If-Match",
    location=OpenApiParameter.HEADER,
    required=False,
    type=int,
    description=(
        "The `version` the client last read. When present and stale the request "
        "is rejected with 409 `version_conflict` instead of silently overwriting "
        "somebody else's change."
    ),
)


def parse_if_match(request: Request) -> int | None:
    """Read the optional optimistic-concurrency token (ADR-07).

    Accepts a bare integer as well as the quoted and weak ETag spellings, so a
    client can echo back whatever it read from the ``ETag`` response header.
    """
    raw = request.headers.get("If-Match")
    if not raw:
        return None
    value = raw.strip()
    if value.startswith(("W/", "w/")):
        value = value[2:]
    value = value.strip().strip('"')
    try:
        return int(value)
    except ValueError:
        raise ValidationError(
            {"If-Match": "Expected the integer ticket version, for example: 3."}
        ) from None


def _rank_expression(field: str, ranks: dict[str, int]) -> Case:
    """Turn a set of labels into the order a human would put them in.

    Sorting the stored strings is alphabetical, which for `URGENT` and `CLOSED`
    is exactly backwards.
    """
    return Case(
        *[When(**{field: value}, then=Value(rank)) for value, rank in ranks.items()],
        default=Value(0),
        output_field=IntegerField(),
    )


@extend_schema(tags=["tickets"])
class TicketViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The agent-facing ticket workflow.

    There is no ``DELETE``: a reported incident is a fact, and destroying it
    destroys the trail that made the system worth building (ADR-13).
    """

    permission_classes = [IsAuthenticated, TicketActionPermission]
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"
    lookup_value_regex = UUID_URL_REGEX
    http_method_names = ["get", "post", "patch", "head", "options"]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, SemanticOrderingFilter]
    filterset_class = TicketFilter
    search_fields = ["subject", "description", "customer__name", "customer__email"]
    ordering_fields = ["created_at", "updated_at", "status", "priority", "first_assigned_at"]
    ordering = ["-created_at"]

    serializer_classes = {
        "list": TicketListSerializer,
        "retrieve": TicketDetailSerializer,
        "create": AgentTicketCreateSerializer,
        "partial_update": TicketUpdateSerializer,
        "change_status": StatusChangeSerializer,
        "change_priority": PriorityChangeSerializer,
        "assign": AssignSerializer,
        "assign_to_me": AssignSerializer,
        "comments": CommentCreateSerializer,
        "timeline": TimelineEntrySerializer,
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, TicketDetailSerializer)

    @property
    def actor(self) -> User:
        """The agent making the request.

        `IsAuthenticated` runs before every handler, so this is never anonymous;
        the cast says so once instead of at each of the eight call sites.
        """
        return cast(User, self.request.user)

    def get_queryset(self) -> QuerySet[Ticket]:
        # select_related on every foreign key the serializers touch: the list
        # endpoint must cost the same two queries whether it returns 1 row or 100.
        return Ticket.objects.select_related("customer", "assignee", "created_by").annotate(
            # No `distinct=True`: every join in this queryset and in every
            # filter above is to-one, so no row is counted twice and the
            # extra de-duplication would be paid on each page for nothing.
            comment_count=Count("comments"),
            priority_rank=_rank_expression("priority", PRIORITY_RANK),
            status_rank=_rank_expression("status", STATUS_RANK),
        )

    @property
    def expected_version(self) -> int | None:
        """The `If-Match` version, parsed once for every write action."""
        return parse_if_match(self.request)

    @property
    def paginator(self) -> BasePagination | None:
        """Append-only feeds get cursors; bounded collections get page numbers."""
        if self.action in {"comments", "timeline"}:
            if not hasattr(self, "_cursor_paginator"):
                self._cursor_paginator = CreatedAtCursorPagination()
            return self._cursor_paginator
        return super().paginator

    def retrieve(self, request: Request, *args, **kwargs) -> Response:
        response = super().retrieve(request, *args, **kwargs)
        # Publishing the version as an ETag lets a client echo it straight back
        # in If-Match without knowing anything about our payload shape.
        response["ETag"] = f'"{response.data["version"]}"'
        return response

    # -- creation -----------------------------------------------------------
    @extend_schema(
        summary="Create a ticket on behalf of a customer",
        description=(
            "Accepts an optional `Idempotency-Key`; a replay answers `200` with "
            "the original ticket instead of creating a second one."
        ),
        request=AgentTicketCreateSerializer,
        responses={
            201: TicketDetailSerializer,
            200: OpenApiResponse(TicketDetailSerializer, description="Duplicate request replayed"),
        },
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.create_ticket(
            subject=data["subject"],
            description=data["description"],
            reported_priority=data["reported_priority"],
            priority=data.get("priority"),
            customer_name=data["customer"]["name"],
            customer_email=data["customer"]["email"],
            actor=self.actor,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        # A replayed Idempotency-Key answers 200, exactly as the public endpoint
        # does: the same request must not mean two different things depending on
        # which door it came through.
        response = self._detail_response(
            result.ticket,
            http_status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )
        if not result.created:
            response["Idempotency-Replayed"] = "true"
        return response

    # -- descriptive edit ---------------------------------------------------
    @extend_schema(
        summary="Edit the descriptive fields",
        description="Only `subject` and `description`. State lives behind its own actions.",
        parameters=[IF_MATCH_PARAMETER],
        request=TicketUpdateSerializer,
        responses={200: TicketDetailSerializer},
    )
    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        ticket = self.get_object()
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_details(
            public_id=ticket.public_id,
            actor=self.actor,
            subject=serializer.validated_data.get("subject"),
            description=serializer.validated_data.get("description"),
            expected_version=self.expected_version,
        )
        return self._detail_response(updated)

    # -- state actions ------------------------------------------------------
    @extend_schema(
        summary="Change the status",
        parameters=[IF_MATCH_PARAMETER],
        request=StatusChangeSerializer,
        responses={
            200: TicketDetailSerializer,
            400: OpenApiResponse(description="`invalid_transition` — includes the allowed set"),
            409: OpenApiResponse(description="`version_conflict` or `ticket_closed`"),
        },
    )
    @action(detail=True, methods=["post"], url_path="status")
    def change_status(self, request: Request, public_id: str | None = None) -> Response:
        ticket = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = services.change_status(
            public_id=ticket.public_id,
            actor=self.actor,
            new_status=serializer.validated_data["status"],
            note=serializer.validated_data["note"],
            expected_version=self.expected_version,
        )
        return self._detail_response(updated)

    @extend_schema(
        summary="Reclassify the priority",
        parameters=[IF_MATCH_PARAMETER],
        request=PriorityChangeSerializer,
        responses={200: TicketDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="priority")
    def change_priority(self, request: Request, public_id: str | None = None) -> Response:
        ticket = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = services.change_priority(
            public_id=ticket.public_id,
            actor=self.actor,
            new_priority=serializer.validated_data["priority"],
            note=serializer.validated_data["note"],
            expected_version=self.expected_version,
        )
        return self._detail_response(updated)

    @extend_schema(
        summary="Assign, reassign or release",
        description="`assignee_id: null` releases the ticket; an `IN_PROGRESS` "
        "ticket then returns to `OPEN` (ADR-14).",
        parameters=[IF_MATCH_PARAMETER],
        request=AssignSerializer,
        responses={200: TicketDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request: Request, public_id: str | None = None) -> Response:
        ticket = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = services.assign(
            public_id=ticket.public_id,
            actor=self.actor,
            assignee=serializer.validated_data["assignee"],
            note=serializer.validated_data["note"],
            expected_version=self.expected_version,
        )
        return self._detail_response(updated)

    @extend_schema(
        summary="Take the ticket",
        description="Shortcut for the only assignment an agent makes all day.",
        parameters=[IF_MATCH_PARAMETER],
        request=None,
        responses={200: TicketDetailSerializer},
    )
    @action(detail=True, methods=["post"], url_path="assign-to-me")
    def assign_to_me(self, request: Request, public_id: str | None = None) -> Response:
        ticket = self.get_object()
        updated = services.assign(
            public_id=ticket.public_id,
            actor=self.actor,
            assignee=self.actor,
            note="Picked up by the agent.",
            expected_version=self.expected_version,
        )
        return self._detail_response(updated)

    # -- comments and history ----------------------------------------------
    @extend_schema(
        methods=["GET"],
        summary="List the comments",
        responses={200: CommentSerializer(many=True)},
    )
    @extend_schema(
        methods=["POST"],
        summary="Add a comment",
        request=CommentCreateSerializer,
        responses={201: CommentSerializer},
    )
    @action(detail=True, methods=["get", "post"], url_path="comments")
    def comments(self, request: Request, public_id: str | None = None) -> Response:
        ticket = self.get_object()
        if request.method == "POST":
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            comment = services.add_comment(
                public_id=ticket.public_id,
                actor=self.actor,
                body=serializer.validated_data["body"],
                is_internal=serializer.validated_data["is_internal"],
                expected_version=self.expected_version,
            )
            return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)

        queryset = Comment.objects.filter(ticket=ticket).select_related("author")
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(CommentSerializer(page, many=True).data)

    @extend_schema(
        summary="The unified history",
        description=(
            "Status changes, assignments, reclassifications and comments in one "
            "chronological feed, discriminated by `kind` (ADR-08)."
        ),
        responses={200: TimelineEntrySerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="timeline")
    def timeline(self, request: Request, public_id: str | None = None) -> Response:
        ticket = self.get_object()
        queryset = TicketEvent.objects.filter(ticket=ticket).select_related("actor", "comment")
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(TimelineEntrySerializer(page, many=True).data)

    # -- helpers ------------------------------------------------------------
    def _detail_response(
        self, ticket: Ticket, *, http_status: int = status.HTTP_200_OK
    ) -> Response:
        """Always answer a mutation with the full, freshly-read ticket.

        The client gets the new `version` and the new `allowed_transitions` in
        the same round trip, so its next request cannot be built on stale state.
        """
        fresh = self.get_queryset().get(pk=ticket.pk)
        response = Response(TicketDetailSerializer(fresh).data, status=http_status)
        response["ETag"] = f'"{fresh.version}"'
        return response


@extend_schema(
    tags=["public"],
    summary="Submit a support request",
    description=(
        "Unauthenticated intake. Throttled per IP and idempotent: send an "
        "`Idempotency-Key` header, or rely on the 60-second duplicate window. "
        "A replayed request answers `200` with the original ticket instead of "
        "creating a second one (ADR-15)."
    ),
    request=PublicTicketCreateSerializer,
    responses={
        201: TicketPublicSerializer,
        200: OpenApiResponse(TicketPublicSerializer, description="Duplicate submission replayed"),
        429: OpenApiResponse(description="`throttled`"),
    },
    parameters=[
        OpenApiParameter(
            name="Idempotency-Key",
            location=OpenApiParameter.HEADER,
            required=False,
            type=str,
            description="Client-generated key; a repeat within 24h returns the same ticket.",
        )
    ],
)
class PublicTicketCreateView(GenericAPIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_ticket_create"
    serializer_class = PublicTicketCreateSerializer

    def post(self, request: Request) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.create_ticket(
            subject=data["subject"],
            description=data["description"],
            reported_priority=data["reported_priority"],
            customer_name=data["customer"]["name"],
            customer_email=data["customer"]["email"],
            actor=None,
            idempotency_key=request.headers.get("Idempotency-Key"),
            deduplicate=True,
        )
        response = Response(
            TicketPublicSerializer(result.ticket).data,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )
        if not result.created:
            response["Idempotency-Replayed"] = "true"
        return response


@extend_schema(
    tags=["public"],
    summary="Track a support request",
    description="Status and dates only. No agent identity, no comments, no history.",
    responses={200: TicketPublicSerializer},
)
class PublicTicketDetailView(RetrieveAPIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_ticket_read"
    serializer_class = TicketPublicSerializer
    queryset = Ticket.objects.all()
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"
