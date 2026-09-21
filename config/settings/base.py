"""Settings shared by every environment.

Environment-specific modules (`local`, `production`, `test`) import * from here
and override only what actually differs. Nothing secret is hardcoded: every
value that matters in production comes from the environment (ADR-18 neighbours:
see `.env.example` for the full list).
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

import environ

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env()
# A local .env is optional: in Docker and CI the variables are already exported.
env.read_env(BASE_DIR / ".env", overwrite=False)

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-insecure-key-replace-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
]
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.customers",
    "apps.tickets",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    # First in the chain so every log line and every error envelope downstream
    # carries the same request id.
    "apps.core.middleware.RequestIDMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://support:support@db:5432/support",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False  # transactions are explicit, in services
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"  # ADR-02

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# I18N / time — ADR-18: store UTC, speak ISO 8601 at the edge
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# No endpoint accepts uploads, so the 2.5 MB Django allows by default is 2.5 MB
# of head room an open endpoint does not need.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=1024 * 1024)

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
REST_FRAMEWORK: dict[str, Any] = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    # Authenticated by default; the two public endpoints opt out explicitly.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    # JSON only. The browsable API is a development convenience, and in
    # production it is an HTML form over every endpoint, including the open one.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # How many proxies sit in front of this process, and the single most
    # important number on this page. DRF's own default (None) trusts
    # X-Forwarded-For whenever it is present, which means anyone can invent a
    # value and get a fresh rate-limit bucket on every request. 0 pins identity
    # to REMOTE_ADDR. A deployment behind N proxies sets it to N so the limit
    # follows the real client instead of collapsing onto the load balancer.
    "NUM_PROXIES": env.int("NUM_PROXIES", default=0),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "public_ticket_create": env("THROTTLE_PUBLIC_CREATE", default="20/hour"),
        "public_ticket_read": env("THROTTLE_PUBLIC_READ", default="120/hour"),
        # Credential stuffing is the cheapest attack against any login form.
        "auth_token": env("THROTTLE_AUTH_TOKEN", default="10/min"),
        # Per reported email, on top of the per-IP limit: the edge sees addresses,
        # not customers, so this is the dimension infrastructure cannot cover.
        "public_ticket_email": env("THROTTLE_PUBLIC_EMAIL", default="5/hour"),
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "DATETIME_FORMAT": "iso-8601",
}

SIMPLE_JWT: dict[str, Any] = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=60)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=7)),
    "ROTATE_REFRESH_TOKENS": False,
    "UPDATE_LAST_LOGIN": True,
    # A JWT payload is base64, not a secret: putting the sequential id in it
    # would leak through the front door what ADR-04 keeps out of the URLs.
    "USER_ID_FIELD": "public_id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS: dict[str, Any] = {
    "TITLE": "Support Request Management API",
    "DESCRIPTION": (
        "REST API used by internal agents to triage, assign and resolve customer "
        "support requests. Every mutation is audited and every resource is addressed "
        "by its public UUID."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    # Deterministic output. Without it the operation order follows a set
    # iteration, which varies with PYTHONHASHSEED between processes — and the
    # CI job that diffs the committed schema would fail at random.
    "SORT_OPERATIONS": True,
    "ENUM_NAME_OVERRIDES": {
        "TicketStatusEnum": "apps.tickets.enums.Status.choices",
        "TicketPriorityEnum": "apps.tickets.enums.Priority.choices",
        "TicketEventTypeEnum": "apps.tickets.enums.EventType.choices",
        "UserRoleEnum": "apps.accounts.enums.Role.choices",
    },
    "TAGS": [
        {"name": "public", "description": "Unauthenticated endpoints used by customers."},
        {"name": "auth", "description": "Token issuing and the current user."},
        {"name": "tickets", "description": "Agent-facing ticket workflow."},
        {"name": "agents", "description": "Internal staff directory."},
        {"name": "ops", "description": "Operational endpoints."},
    ],
}

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])

# ---------------------------------------------------------------------------
# Logging — structured JSON, one line per event, always with the request id
# ---------------------------------------------------------------------------
LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="INFO")
LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.core.logging.RequestIDFilter"},
    },
    "formatters": {
        "json": {"()": "apps.core.logging.JSONFormatter"},
        "console": {
            "format": "%(levelname)s %(asctime)s [%(request_id)s] %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "apps": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Domain knobs (ADR-15)
# ---------------------------------------------------------------------------
PUBLIC_TICKET_DEDUPE_WINDOW_SECONDS = env.int("PUBLIC_TICKET_DEDUPE_WINDOW_SECONDS", default=60)
IDEMPOTENCY_KEY_WINDOW_SECONDS = env.int("IDEMPOTENCY_KEY_WINDOW_SECONDS", default=86_400)

#: Whether the deployment must refuse to boot on a per-process cache. Throttle
#: counters live in the cache, so a local one silently multiplies every limit by
#: the number of workers. False here, True in production.
REQUIRE_SHARED_CACHE = env.bool("REQUIRE_SHARED_CACHE", default=False)

#: Password handed to every account created by `manage.py seed_demo` (demo only).
DEMO_PASSWORD = env("DEMO_PASSWORD", default="demo12345")
