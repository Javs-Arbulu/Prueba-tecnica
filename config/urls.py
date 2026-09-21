"""URL map.

Everything lives under ``/api/v1/``. The version is in the path rather than in a
header because it is the one place a reviewer, a curl command and a browser all
agree on.
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import AgentListView, MeView, SupportTokenObtainPairView
from apps.core.views import APIRootView, HealthView
from apps.tickets.views import PublicTicketCreateView, PublicTicketDetailView, TicketViewSet

# SimpleRouter, not DefaultRouter: the index below replaces its root view,
# which would only ever list the registered viewsets.
router = SimpleRouter()
router.register("tickets", TicketViewSet, basename="ticket")

public_patterns = [
    path("tickets/", PublicTicketCreateView.as_view(), name="public-ticket-create"),
    path(
        "tickets/<uuid:public_id>/",
        PublicTicketDetailView.as_view(),
        name="public-ticket-detail",
    ),
]

auth_patterns = [
    path("token/", SupportTokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
]

api_v1_patterns = [
    path("", APIRootView.as_view(), name="api-root"),
    path("public/", include(public_patterns)),
    path("auth/", include(auth_patterns)),
    path("me/", MeView.as_view(), name="me"),
    path("agents/", AgentListView.as_view(), name="agent-list"),
    path("health/", HealthView.as_view(), name="health"),
    *router.urls,
]

# Every failure answers with the same envelope, including the ones Django handles
# before DRF is involved. See apps/core/views.py.
handler400 = "apps.core.views.bad_request"
handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1_patterns)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
