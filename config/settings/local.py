"""Developer machine / docker compose defaults."""

from .base import *

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True

# Console formatter is easier to read than JSON while developing.
LOGGING["handlers"]["console"]["formatter"] = "console"
