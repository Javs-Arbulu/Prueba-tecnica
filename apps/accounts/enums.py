from django.db import models


class Role(models.TextChoices):
    """Who the internal user is, which is the only axis permissions depend on.

    Two roles are enough to express the rules the use case actually has. A third
    one would need a reason, not a hunch.
    """

    AGENT = "AGENT", "Agent"
    SUPERVISOR = "SUPERVISOR", "Supervisor"
