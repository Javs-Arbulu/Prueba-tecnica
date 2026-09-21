"""Cross-cutting behaviour: authentication, health, correlation ids, error shape."""

from __future__ import annotations

import base64
import json
from unittest import mock

import pytest
from django.db import connection
from django.db.utils import OperationalError
from rest_framework.throttling import SimpleRateThrottle

from apps.core.exceptions import api_exception_handler
from tests.factories import DEFAULT_PASSWORD

pytestmark = pytest.mark.django_db


def test_a_token_carries_the_role_and_the_login_returns_the_user(api_client, supervisor):
    response = api_client.post(
        "/api/v1/auth/token/", {"email": supervisor.email, "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 200
    assert response.data["user"]["role"] == "SUPERVISOR"
    assert {"access", "refresh"} <= set(response.data)


def test_a_wrong_password_is_a_401_in_the_standard_envelope(api_client, agent):
    response = api_client.post(
        "/api/v1/auth/token/", {"email": agent.email, "password": "not-the-password"}
    )

    assert response.status_code == 401
    assert response.data["error"]["code"] == "not_authenticated"


def test_the_access_token_opens_the_agent_api(api_client, agent):
    token = api_client.post(
        "/api/v1/auth/token/", {"email": agent.email, "password": DEFAULT_PASSWORD}
    ).data["access"]

    response = api_client.get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 200
    assert response.data["email"] == agent.email
    assert "id" not in response.data  # only the public id ever leaves (ADR-04)


def test_a_refresh_token_produces_a_new_access_token(api_client, agent):
    refresh = api_client.post(
        "/api/v1/auth/token/", {"email": agent.email, "password": DEFAULT_PASSWORD}
    ).data["refresh"]

    response = api_client.post("/api/v1/auth/token/refresh/", {"refresh": refresh})

    assert response.status_code == 200
    assert "access" in response.data


def test_health_reports_the_database(api_client):
    response = api_client.get("/api/v1/health/")

    assert response.status_code == 200
    assert response.data == {
        "status": "ok",
        "database": "ok",
        "time": response.data["time"],
    }


def test_every_response_carries_a_request_id(api_client):
    response = api_client.get("/api/v1/health/")

    assert response["X-Request-ID"]


def test_a_client_supplied_request_id_is_propagated(api_client):
    response = api_client.get("/api/v1/health/", HTTP_X_REQUEST_ID="trace-abc-123")

    assert response["X-Request-ID"] == "trace-abc-123"


def test_a_hostile_request_id_is_replaced_not_echoed(api_client):
    response = api_client.get("/api/v1/health/", HTTP_X_REQUEST_ID="<script>alert(1)</script>")

    assert response["X-Request-ID"] != "<script>alert(1)</script>"


def test_error_payloads_quote_the_request_id_that_produced_them(api_client):
    response = api_client.get("/api/v1/tickets/", HTTP_X_REQUEST_ID="trace-abc-123")

    assert response.status_code == 401
    assert response.data["error"]["request_id"] == "trace-abc-123"
    assert set(response.data["error"]) == {"code", "message", "details", "request_id"}


def test_the_openapi_schema_is_served_and_documents_every_endpoint(api_client):
    response = api_client.get("/api/schema/?format=json")

    assert response.status_code == 200
    schema = json.loads(response.content)
    documented = set(schema["paths"])
    assert {
        "/api/v1/tickets/",
        "/api/v1/tickets/{public_id}/",
        "/api/v1/tickets/{public_id}/status/",
        "/api/v1/tickets/{public_id}/assign/",
        "/api/v1/tickets/{public_id}/assign-to-me/",
        "/api/v1/tickets/{public_id}/priority/",
        "/api/v1/tickets/{public_id}/comments/",
        "/api/v1/tickets/{public_id}/timeline/",
        "/api/v1/public/tickets/",
        "/api/v1/public/tickets/{public_id}/",
        "/api/v1/auth/token/",
        "/api/v1/me/",
        "/api/v1/agents/",
        "/api/v1/health/",
    } <= documented


def test_the_token_never_carries_the_internal_id(api_client, agent):
    """ADR-04 holds at the front door too: a JWT payload is base64, not a secret."""
    access = api_client.post(
        "/api/v1/auth/token/", {"email": agent.email, "password": DEFAULT_PASSWORD}
    ).data["access"]

    payload = json.loads(
        base64.urlsafe_b64decode(access.split(".")[1] + "=" * (-len(access.split(".")[1]) % 4))
    )

    assert payload["user_id"] == str(agent.public_id)
    assert payload["role"] == agent.role
    assert str(agent.pk) not in [str(value) for value in payload.values()]


def test_the_login_endpoint_is_throttled(api_client, agent, monkeypatch):
    """Credential stuffing is the cheapest attack there is against a login form."""
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", {"auth_token": "3/min"})
    credentials = {"email": agent.email, "password": "wrong-password"}

    statuses = [api_client.post("/api/v1/auth/token/", credentials).status_code for _ in range(4)]

    assert statuses == [401, 401, 401, 429]


def test_health_reports_a_broken_database_with_503(api_client):
    with mock.patch.object(connection, "cursor", side_effect=OperationalError("down")):
        response = api_client.get("/api/v1/health/")

    assert response.status_code == 503
    assert response.data["status"] == "degraded"
    assert response.data["database"] == "unreachable"


def test_an_unhandled_exception_still_answers_the_error_contract():
    """A crash must not become the one response shape a client cannot parse."""
    response = api_exception_handler(RuntimeError("boom"), {"view": "TicketViewSet"})

    assert response.status_code == 500
    assert response.data["error"]["code"] == "server_error"
    assert set(response.data["error"]) == {"code", "message", "details", "request_id"}
    assert "boom" not in response.data["error"]["message"]  # internals stay internal


def test_the_api_root_maps_every_entry_point(api_client):
    """Whoever opens the base URL should find the whole API, not one link."""
    response = api_client.get("/api/v1/")

    assert response.status_code == 200
    assert set(response.data) == {
        "documentation",
        "public",
        "authentication",
        "agents",
        "operations",
    }
    assert response.data["documentation"]["swagger"].endswith("/api/docs/")
    assert response.data["public"]["submit_request"].endswith("/api/v1/public/tickets/")


def test_options_describes_the_endpoint_instead_of_refusing(agent_client, ticket):
    """DRF calls the OPTIONS handler `metadata`; describing a route is a read."""
    on_list = agent_client.options("/api/v1/tickets/")
    on_detail = agent_client.options(f"/api/v1/tickets/{ticket.public_id}/")

    assert (on_list.status_code, on_detail.status_code) == (200, 200)
    assert "actions" in on_list.data


def test_a_body_that_is_not_json_gets_its_own_error_code(api_client):
    """`validation_error.details.fields` must always be an object, so a body that
    never parsed cannot borrow that code."""
    response = api_client.post(
        "/api/v1/public/tickets/", data="{not json", content_type="application/json"
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "malformed_request"
    assert response.data["error"]["details"] == {}


def test_the_agent_directory_is_not_hidden_behind_pagination(agent_client, supervisor):
    """A picker that shows 20 of 50 agents is a bug found at the worst moment."""
    from tests.factories import UserFactory

    for _ in range(25):
        UserFactory()

    response = agent_client.get("/api/v1/agents/")

    assert response.data["next"] is None
    assert len(response.data["results"]) == response.data["count"]
