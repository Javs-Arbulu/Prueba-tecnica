from __future__ import annotations

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.tickets.models import Ticket
from tests.factories import CustomerFactory, SupervisorFactory, TicketFactory, UserFactory


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """Throttling counters live in the cache; a leaked counter fails the next test."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def agent(db):
    return UserFactory(email="ana@support.local", first_name="Ana", last_name="Reyes")


@pytest.fixture
def other_agent(db):
    return UserFactory(email="luis@support.local", first_name="Luis", last_name="Ferrer")


@pytest.fixture
def supervisor(db):
    return SupervisorFactory(email="marta@support.local", first_name="Marta", last_name="Ibarra")


@pytest.fixture
def agent_client(api_client, agent) -> APIClient:
    api_client.force_authenticate(agent)
    return api_client


@pytest.fixture
def supervisor_client(api_client, supervisor) -> APIClient:
    api_client.force_authenticate(supervisor)
    return api_client


@pytest.fixture
def customer(db):
    return CustomerFactory(name="Elena Duarte", email="elena@northwind.example")


@pytest.fixture
def ticket(db, customer) -> Ticket:
    return TicketFactory(customer=customer)


def ticket_url(ticket: Ticket, suffix: str = "") -> str:
    return f"/api/v1/tickets/{ticket.public_id}/{suffix}"
