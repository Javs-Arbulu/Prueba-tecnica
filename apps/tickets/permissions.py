"""Who may do what, declared as data.

The authorisation rules of this system fit in one table. Written as a table they
can be read at a glance, reviewed by somebody non-technical and tested
exhaustively; scattered as ``if request.user ...`` across seven view methods they
can only be audited by reading every view (ADR-07 neighbour: one permission
class, matrix as data).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.enums import Role
from apps.accounts.models import User
from apps.tickets.models import Ticket


class Action(StrEnum):
    VIEW = "view"
    CREATE = "create"
    UPDATE_DETAILS = "update_details"
    CHANGE_STATUS = "change_status"
    CHANGE_PRIORITY = "change_priority"
    ASSIGN = "assign"
    COMMENT = "comment"


@dataclass(frozen=True)
class AccessContext:
    """Everything a rule is allowed to look at.

    The assignment target is carried as a public id rather than a ``User``: the
    rules only ever ask "is this me?", so the permission layer never needs a
    database round trip to answer.
    """

    actor: User
    ticket: Ticket | None = None
    has_target: bool = False
    target_public_id: str | None = None  # None with has_target=True means "release"


Rule = Callable[[AccessContext], bool]


def _always(context: AccessContext) -> bool:
    return True


def _owner_or_unclaimed(context: AccessContext) -> bool:
    """An agent drives the tickets they own and may pick up free ones.

    They cannot reach into a colleague's work in progress; that is what the
    supervisor role exists for.
    """
    ticket = context.ticket
    if ticket is None:
        return True
    return ticket.assignee_id in (None, context.actor.id)


def _self_service_assignment(context: AccessContext) -> bool:
    """An agent can take an unclaimed ticket and can let go of their own.

    Handing work to somebody else is a scheduling decision, so it belongs to a
    supervisor.
    """
    ticket = context.ticket
    if ticket is None or not context.has_target:
        return True
    if context.target_public_id is None:  # releasing the ticket
        return ticket.assignee_id == context.actor.id
    if context.target_public_id != str(context.actor.public_id):  # assigning a third party
        return False
    return ticket.assignee_id in (None, context.actor.id)


#: The complete authorisation model of the application.
ACCESS_MATRIX: dict[Action, dict[str, Rule]] = {
    Action.VIEW: {Role.AGENT: _always, Role.SUPERVISOR: _always},
    Action.CREATE: {Role.AGENT: _always, Role.SUPERVISOR: _always},
    Action.COMMENT: {Role.AGENT: _always, Role.SUPERVISOR: _always},
    Action.CHANGE_PRIORITY: {Role.AGENT: _always, Role.SUPERVISOR: _always},
    Action.UPDATE_DETAILS: {Role.AGENT: _owner_or_unclaimed, Role.SUPERVISOR: _always},
    Action.CHANGE_STATUS: {Role.AGENT: _owner_or_unclaimed, Role.SUPERVISOR: _always},
    Action.ASSIGN: {Role.AGENT: _self_service_assignment, Role.SUPERVISOR: _always},
}

#: Viewset action -> domain action. ``comments`` depends on the HTTP verb.
VIEW_ACTION_MAP: dict[str, Action] = {
    "list": Action.VIEW,
    "retrieve": Action.VIEW,
    "timeline": Action.VIEW,
    # DRF names the OPTIONS handler "metadata"; describing an endpoint is a read.
    "metadata": Action.VIEW,
    "create": Action.CREATE,
    "partial_update": Action.UPDATE_DETAILS,
    "change_status": Action.CHANGE_STATUS,
    "change_priority": Action.CHANGE_PRIORITY,
    "assign": Action.ASSIGN,
    "assign_to_me": Action.ASSIGN,
}

DENIAL_MESSAGES: dict[Action, str] = {
    Action.CHANGE_STATUS: "Only the assigned agent or a supervisor can change this ticket.",
    Action.UPDATE_DETAILS: "Only the assigned agent or a supervisor can edit this ticket.",
    Action.ASSIGN: "Only a supervisor can assign a ticket to another agent.",
}

DEFAULT_DENIAL_MESSAGE = "You do not have permission to perform this action on this ticket."


def is_allowed(action: Action, context: AccessContext) -> bool:
    rule = ACCESS_MATRIX.get(action, {}).get(context.actor.role)
    return bool(rule and rule(context))


class TicketActionPermission(BasePermission):
    """Adapter between DRF's hooks and :data:`ACCESS_MATRIX`."""

    message = DEFAULT_DENIAL_MESSAGE

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.role not in ACCESS_MATRIX[Action.VIEW]:
            return False
        if getattr(view, "action", None) is None:
            # No handler is routed for this method (DELETE, PUT...). Letting it
            # through produces an honest 405 instead of a 403 that implies the
            # operation exists and is merely forbidden.
            return True
        action = self._resolve_action(request, view)
        if action is None:
            return False
        # Rules that need the ticket run again in has_object_permission, once
        # DRF has fetched it.
        return is_allowed(action, AccessContext(actor=user))

    def has_object_permission(self, request: Request, view: APIView, obj: Ticket) -> bool:
        action = self._resolve_action(request, view)
        if action is None:
            return False
        has_target, target_public_id = self._resolve_target(request, view)
        allowed = is_allowed(
            action,
            AccessContext(
                # IsAuthenticated runs first, so this is always a real user.
                actor=cast(User, request.user),
                ticket=obj,
                has_target=has_target,
                target_public_id=target_public_id,
            ),
        )
        if not allowed:
            self.message = DENIAL_MESSAGES.get(action, DEFAULT_DENIAL_MESSAGE)
        return allowed

    @staticmethod
    def _resolve_action(request: Request, view: APIView) -> Action | None:
        view_action = getattr(view, "action", None)
        if view_action == "comments":
            return Action.COMMENT if request.method == "POST" else Action.VIEW
        return VIEW_ACTION_MAP.get(view_action or "")

    @staticmethod
    def _resolve_target(request: Request, view: APIView) -> tuple[bool, str | None]:
        view_action = getattr(view, "action", None)
        if view_action == "assign_to_me":
            return True, str(cast(User, request.user).public_id)
        if view_action != "assign":
            return False, None
        raw = request.data.get("assignee_id") if hasattr(request, "data") else None
        return True, None if raw in (None, "") else str(raw)
