"""Developer machine / docker compose defaults."""

from .base import *

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True

# The browsable API is genuinely useful while developing, and only here.
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]

# Console formatter is easier to read than JSON while developing.
LOGGING["handlers"]["console"]["formatter"] = "console"
