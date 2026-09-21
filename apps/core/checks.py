"""Startup checks for the things that fail quietly.

A misconfiguration that halves a security control without saying anything is
worse than one that stops the boot, because nobody finds it until it matters.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import Error, register

#: Backends that keep their data inside a single process.
_PER_PROCESS_BACKENDS = ("locmem", "dummy")


@register()
def shared_cache_is_configured(app_configs: Any, **kwargs: Any) -> list[Error]:
    """Throttle counters live in the cache, so a per-process cache multiplies
    every rate limit by the number of workers, silently."""
    if not getattr(settings, "REQUIRE_SHARED_CACHE", False):
        return []

    backend = settings.CACHES["default"]["BACKEND"]
    if not any(marker in backend for marker in _PER_PROCESS_BACKENDS):
        return []

    return [
        Error(
            "Rate limiting needs a cache shared by every worker.",
            hint=(
                f"CACHES['default']['BACKEND'] is {backend}, which is per process: "
                "with N workers every throttle allows N times its configured rate. "
                "Point it at Redis or Memcached, or set REQUIRE_SHARED_CACHE=False "
                "if this deployment really does run a single process."
            ),
            id="core.E001",
        )
    ]
