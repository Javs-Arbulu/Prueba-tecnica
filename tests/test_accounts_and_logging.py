"""The small pieces everything else stands on."""

from __future__ import annotations

import json
import logging

import pytest

from apps.accounts.enums import Role
from apps.accounts.models import User
from apps.core.logging import JSONFormatter, RequestIDFilter
from apps.customers.models import Customer

pytestmark = pytest.mark.django_db


def test_users_are_created_from_an_email_not_a_username():
    user = User.objects.create_user(email="New.Agent@Support.local", password="s3cret-pass")

    assert user.email == "New.Agent@support.local"  # domain normalised by Django
    assert user.role == Role.AGENT
    assert user.check_password("s3cret-pass")
    assert user.is_staff is False


def test_a_user_without_an_email_is_refused():
    with pytest.raises(ValueError, match="email"):
        User.objects.create_user(email="", password="whatever")


def test_a_superuser_is_a_supervisor():
    user = User.objects.create_superuser(email="root@support.local", password="s3cret-pass")

    assert (user.is_staff, user.is_superuser) == (True, True)
    assert user.is_supervisor is True


def test_a_superuser_cannot_be_created_without_its_flags():
    with pytest.raises(ValueError, match="is_staff"):
        User.objects.create_superuser(
            email="fake@support.local", password="s3cret-pass", is_staff=False
        )


def test_the_display_name_falls_back_to_the_email():
    user = User.objects.create_user(email="nameless@support.local", password="s3cret-pass")

    assert user.display_name == "nameless@support.local"
    assert str(user) == "nameless@support.local"


def test_customer_emails_are_stored_normalised():
    """Two spellings of one address must not become two customers."""
    customer = Customer.objects.create(name="Elena", email="  Elena.Duarte@Northwind.EXAMPLE ")

    customer.refresh_from_db()
    assert customer.email == "elena.duarte@northwind.example"
    assert str(customer) == "Elena <elena.duarte@northwind.example>"

    # The same normalisation runs through full_clean(), for the admin and forms.
    from_form = Customer(name="Elena again", email="ELENA.duarte@northwind.example")
    from_form.clean()
    assert from_form.email == "elena.duarte@northwind.example"


def test_log_lines_are_json_and_carry_the_request_id():
    record = logging.LogRecord(
        name="apps.request",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="GET %s %s",
        args=("/api/v1/tickets/", 200),
        exc_info=None,
    )
    record.status_code = 200
    RequestIDFilter().filter(record)

    payload = json.loads(JSONFormatter().format(record))

    assert payload["message"] == "GET /api/v1/tickets/ 200"
    assert payload["level"] == "INFO"
    assert payload["status_code"] == 200
    assert payload["request_id"] == "-"  # no request in flight
