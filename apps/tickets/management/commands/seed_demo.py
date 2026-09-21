"""Populate a demo dataset that looks like a support desk in use.

A reviewer who has to create a user, then a customer, then a ticket, then five
transitions before they can look at anything is a reviewer who never gets to the
interesting part. The seed goes through the service layer on purpose: the
histories it produces are real histories, not fixtures pretending to be.
"""

from __future__ import annotations

import random
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.enums import Role
from apps.accounts.models import User
from apps.customers.models import Customer
from apps.tickets import services
from apps.tickets.enums import Priority, Status
from apps.tickets.models import Comment, Ticket, TicketEvent

STAFF = [
    ("supervisor@support.local", "Marta", "Ibarra", Role.SUPERVISOR),
    ("ana.reyes@support.local", "Ana", "Reyes", Role.AGENT),
    ("luis.ferrer@support.local", "Luis", "Ferrer", Role.AGENT),
]

CUSTOMERS = [
    ("Elena Duarte", "elena.duarte@northwind.example"),
    ("Tomás Iglesias", "tomas.iglesias@acme.example"),
    ("Priya Raman", "priya.raman@globex.example"),
    ("Óscar Villa", "oscar.villa@initech.example"),
    ("Hannah Bauer", "hannah.bauer@umbrella.example"),
    ("Marco Pineda", "marco.pineda@soylent.example"),
]

REQUESTS = [
    (
        "Cannot log in after the password reset",
        "I requested a password reset yesterday and the link in the email says it has expired. "
        "I have tried three times from two different browsers.",
        Priority.HIGH,
    ),
    (
        "Invoice 4412 shows the wrong VAT rate",
        "The invoice we received this morning applies 21% VAT, but our contract states 10%. "
        "We need a corrected document before we can pay it.",
        Priority.MEDIUM,
    ),
    (
        "Export to CSV times out on large reports",
        "Exporting the quarterly report never finishes. The spinner runs for about two minutes "
        "and then the page shows a generic error.",
        Priority.HIGH,
    ),
    (
        "Add a bulk upload option for contacts",
        "We onboard around 300 contacts every month and adding them one by one is not workable. "
        "A CSV import would save us days.",
        Priority.LOW,
    ),
    (
        "Mobile app crashes when opening notifications",
        "On Android 14 the app closes immediately after tapping the bell icon. It happens on every "
        "device in our team.",
        Priority.URGENT,
    ),
    (
        "Two-factor codes arrive several minutes late",
        "SMS codes take between three and eight minutes to arrive, by which time they "
        "have expired. This affects our whole finance team.",
        Priority.URGENT,
    ),
    (
        "Request for a data processing agreement",
        "Our legal department needs a signed DPA before we can extend the contract. Who should we "
        "contact about this?",
        Priority.LOW,
    ),
    (
        "Dashboard numbers do not match the export",
        "The dashboard reports 1,204 orders for last week while the CSV export lists 1,187. "
        "Which one should we trust?",
        Priority.MEDIUM,
    ),
    (
        "API returns 500 on the /orders endpoint",
        "Since Tuesday roughly one in ten calls to /v2/orders returns a 500. Retrying the same "
        "request usually works.",
        Priority.URGENT,
    ),
    (
        "Cannot remove a deactivated user from billing",
        "We deactivated a user last month but they are still counted in the seat total we are "
        "being billed for.",
        Priority.MEDIUM,
    ),
    (
        "Attachments over 10 MB are silently dropped",
        "When we attach a file larger than 10 MB the form submits successfully but the attachment "
        "never appears on the ticket.",
        Priority.HIGH,
    ),
    (
        "Spanish translation missing in the checkout",
        "Half of the checkout screen is still in English for our Spanish customers, which is "
        "causing confusion and abandoned carts.",
        Priority.LOW,
    ),
]

INTERNAL_NOTES = [
    "Reproduced on staging with the customer's account. Logs point to the session service.",
    "Asked the customer for the exact timestamp; waiting on their reply.",
    "Escalated to the platform team, they are already tracking a related incident.",
    "Root cause is the cache invalidation we shipped last Thursday. Fix is in review.",
    "Workaround sent to the customer so they are unblocked while we ship the real fix.",
    "Duplicate of an issue reported by another account last week; same stack trace.",
]

RESOLUTIONS = [
    "Fix deployed and confirmed with the customer.",
    "Configuration corrected on the customer's tenant.",
    "Closed after the customer confirmed the workaround is enough for now.",
    "Released in 2.14.1; the customer verified it this morning.",
]


class Command(BaseCommand):
    help = "Create a realistic demo dataset (staff, customers, tickets with history)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing tickets, comments, events and customers first.",
        )
        parser.add_argument(
            "--tickets", type=int, default=25, help="How many tickets to create (default: 25)."
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        random.seed(20260101)  # deterministic demos are easier to talk about

        if options["reset"]:
            TicketEvent.objects.all().delete()
            Comment.objects.all().delete()
            Ticket.objects.all().delete()
            Customer.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing demo data removed."))

        staff = self._ensure_staff()
        if Ticket.objects.exists() and not options["reset"]:
            self.stdout.write(
                self.style.WARNING("Tickets already exist; skipping. Use --reset to rebuild.")
            )
            self._report(staff)
            return

        agents = [user for user in staff if user.role == Role.AGENT]
        supervisor = next(user for user in staff if user.role == Role.SUPERVISOR)

        for index in range(options["tickets"]):
            subject, description, reported = REQUESTS[index % len(REQUESTS)]
            customer_name, customer_email = CUSTOMERS[index % len(CUSTOMERS)]
            suffix = f" (case {index + 1})" if index >= len(REQUESTS) else ""
            # Two thirds arrive through the public form, the rest by phone.
            intake_agent = None if index % 3 else random.choice(agents)
            result = services.create_ticket(
                subject=f"{subject}{suffix}",
                description=description,
                reported_priority=reported,
                customer_name=customer_name,
                customer_email=customer_email,
                actor=intake_agent,
            )
            self._play_out(result.ticket, agents=agents, supervisor=supervisor, index=index)

        self._report(staff)

    # -- helpers ------------------------------------------------------------
    def _ensure_staff(self) -> list[User]:
        staff: list[User] = []
        for email, first_name, last_name, role in STAFF:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": first_name,
                    "last_name": last_name,
                    "role": role,
                    "is_staff": True,  # so the read-only admin is reachable
                    "is_superuser": role == Role.SUPERVISOR,
                },
            )
            if created:
                user.set_password(settings.DEMO_PASSWORD)
                user.save(update_fields=["password"])
            staff.append(user)
        return staff

    def _play_out(
        self, ticket: Ticket, *, agents: list[User], supervisor: User, index: int
    ) -> None:
        """Walk the ticket through a plausible slice of its life."""
        owner = agents[index % len(agents)]
        stage = index % 6

        if stage == 0:  # left in the queue, untouched
            return

        if index % 5 == 0:  # triage disagreed with the customer
            services.change_priority(
                public_id=ticket.public_id,
                actor=supervisor,
                new_priority=random.choice([Priority.LOW, Priority.MEDIUM, Priority.HIGH]),
                note="Reclassified during the morning triage.",
            )

        services.assign(public_id=ticket.public_id, actor=supervisor, assignee=owner)
        services.change_status(
            public_id=ticket.public_id,
            actor=owner,
            new_status=Status.IN_PROGRESS,
            note="Picked up from the queue.",
        )
        services.add_comment(
            public_id=ticket.public_id, actor=owner, body=random.choice(INTERNAL_NOTES)
        )
        if stage == 1:
            return

        if stage == 2:
            services.change_status(
                public_id=ticket.public_id,
                actor=owner,
                new_status=Status.PENDING_CUSTOMER,
                note="Waiting for the customer to send the requested logs.",
            )
            return

        if stage == 3:  # released back to the queue (ADR-14)
            services.assign(
                public_id=ticket.public_id,
                actor=supervisor,
                assignee=None,
                note="Owner is out of office; back to the queue.",
            )
            return

        services.change_status(
            public_id=ticket.public_id,
            actor=owner,
            new_status=Status.RESOLVED,
            note=random.choice(RESOLUTIONS),
        )
        if stage == 4:
            return

        services.add_comment(
            public_id=ticket.public_id,
            actor=supervisor,
            body="Customer confirmed by email. Closing.",
        )
        services.change_status(
            public_id=ticket.public_id,
            actor=supervisor,
            new_status=Status.CLOSED,
            note="Closed after customer confirmation.",
        )

    def _report(self, staff: list[User]) -> None:
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"  customers: {Customer.objects.count()}")
        self.stdout.write(f"  tickets:   {Ticket.objects.count()}")
        self.stdout.write(f"  comments:  {Comment.objects.count()}")
        self.stdout.write(f"  events:    {TicketEvent.objects.count()}")
        self.stdout.write("  sign in with any of:")
        for user in staff:
            self.stdout.write(f"    {user.email} / {settings.DEMO_PASSWORD}  ({user.role})")
