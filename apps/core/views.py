from __future__ import annotations

from django.db import connection
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness plus a real dependency check.

    A health endpoint that only proves the process is up tells you nothing you
    did not already know; this one opens the database connection.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["ops"],
        summary="Liveness and database connectivity",
        responses={200: dict, 503: dict},
        examples=[
            OpenApiExample(
                "healthy",
                value={"status": "ok", "database": "ok", "time": "2026-01-01T00:00:00Z"},
                response_only=True,
            )
        ],
    )
    def get(self, request: Request) -> Response:
        database_ok = self._database_reachable()
        payload = {
            "status": "ok" if database_ok else "degraded",
            "database": "ok" if database_ok else "unreachable",
            "time": timezone.now().isoformat(),
        }
        http_status = status.HTTP_200_OK if database_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(payload, status=http_status)

    @staticmethod
    def _database_reachable() -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            return False
        return True
