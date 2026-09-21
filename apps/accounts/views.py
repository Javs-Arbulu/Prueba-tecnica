from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import filters, generics
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.models import User
from apps.accounts.serializers import SupportTokenObtainPairSerializer, UserSerializer


@extend_schema(tags=["auth"], summary="Obtain an access/refresh token pair")
class SupportTokenObtainPairView(TokenObtainPairView):
    """Throttled: an unauthenticated endpoint that checks passwords is the
    cheapest thing in the system to attack."""

    serializer_class = SupportTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_token"


@extend_schema(tags=["auth"], summary="The authenticated user")
class MeView(generics.RetrieveAPIView):
    serializer_class = UserSerializer

    def get_object(self) -> User:
        return self.request.user  # type: ignore[return-value]


@extend_schema(tags=["agents"], summary="Internal agents available for assignment")
class AgentListView(generics.ListAPIView):
    """Feeds the assignment picker. Inactive staff are never offered."""

    serializer_class = UserSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["first_name", "last_name", "email"]
    ordering_fields = ["first_name", "last_name", "email"]
    queryset = User.objects.filter(is_active=True)
