"""Settings used by pytest.

Fast password hashing, a predictable throttle backend and a real Postgres, so
the database-level constraints from the model layer are exercised for real.
"""

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["*", "testserver"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "throttling",
    }
}

# Throttling is asserted explicitly with @override_settings in the test that
# covers it; a generous default keeps it out of the way everywhere else.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "public_ticket_create": "1000/hour",
    "public_ticket_read": "1000/hour",
}

LOGGING["root"]["level"] = "CRITICAL"
LOGGING["loggers"]["apps"]["level"] = "CRITICAL"
