from __future__ import annotations

import uuid
from typing import Any

from django.db import models


class Customer(models.Model):
    """The external party a ticket belongs to.

    A ``Customer`` is an identity, not an account: no password, no login, no
    account recovery (ADR-03). The email is the natural key, so it is stored
    normalised — ``Ana@Corp.com`` and ``ana@corp.com`` are the same person, and
    a case-sensitive unique index would happily let both exist.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name", "email")

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Normalising in save() rather than only in clean(): DRF serializers do
        # not call full_clean(), and an invariant that depends on the caller
        # remembering to call it is not an invariant.
        self.email = self.normalise_email(self.email)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.email = self.normalise_email(self.email)

    @staticmethod
    def normalise_email(email: str) -> str:
        return (email or "").strip().lower()
