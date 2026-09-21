"""Developer machine / docker compose defaults."""

from .base import *

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True

# No BrowsableAPIRenderer, deliberately. It is a pleasant development toy, but
# it makes local answer HTML where production answers JSON, and an environment
# that disagrees with production about the shape of a response hides bugs
# instead of surfacing them. Swagger at /api/docs/ covers the "click around the
# API" need without changing what the API returns.

# Console formatter is easier to read than JSON while developing.
LOGGING["handlers"]["console"]["formatter"] = "console"
