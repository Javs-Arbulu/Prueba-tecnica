"""Object mothers for the tests.

``make_ticket`` writes a ticket straight into a given state on purpose: the
state machine has to be tested from every state, including the ones a service
call would refuse to leave the ticket in.
"""

from __future__ import annotations

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.accounts.enums import Role
from apps.accounts.models import User
from apps.customers.models import Customer
from apps.tickets.enums import Priority, Status
from apps.tickets.models import Ticket

DEFAULT_PASSWORD = "test-password-123"


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"agent{n}@support.local")
    first_name = factory.Sequence(lambda n: f"Agent{n}")
    last_name = "Doe"
    role = Role.AGENT
    is_active = True

    @factory.post_generation
    def password(obj, create, extracted, **kwargs):
        obj.set_password(extracted or DEFAULT_PASSWORD)
        if create:
            obj.save(update_fields=["password"])


class SupervisorFactory(UserFactory):
    email = factory.Sequence(lambda n: f"supervisor{n}@support.local")
    role = Role.SUPERVISOR


class CustomerFactory(DjangoModelFactory):
    class Meta:
        model = Customer
        django_get_or_create = ("email",)

    name = factory.Sequence(lambda n: f"Customer {n}")
    email = factory.Sequence(lambda n: f"customer{n}@client.example")


class TicketFactory(DjangoModelFactory):
    class Meta:
        model = Ticket

    subject = factory.Sequence(lambda n: f"Something is broken #{n}")
    description = "A description long enough to pass validation."
    reported_priority = Priority.MEDIUM
    priority = Priority.MEDIUM
    status = Status.OPEN
    customer = factory.SubFactory(CustomerFactory)


def make_ticket(status: str = Status.OPEN, *, assignee: User | None = None, **kwargs) -> Ticket:
    """Build a ticket already sitting in ``status``, satisfying the DB constraints."""
    now = timezone.now()
    extra: dict = {}
    if status == Status.RESOLVED:
        extra["resolved_at"] = now
    if status == Status.CLOSED:
        extra["closed_at"] = now
    if status == Status.IN_PROGRESS and assignee is None:
        assignee = UserFactory()
    if assignee is not None:
        extra["first_assigned_at"] = now
    return TicketFactory(status=status, assignee=assignee, **extra, **kwargs)
