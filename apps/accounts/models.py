from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.enums import Role


class UserManager(BaseUserManager["User"]):
    """Email-first manager: there is no username to create users with."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("Users must have an email address.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.SUPERVISOR)
        if extra["is_staff"] is not True or extra["is_superuser"] is not True:
            raise ValueError("A superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Internal staff member.

    Customers are deliberately *not* users of this system (ADR-03): they never
    authenticate, so giving them credentials would only add attack surface.
    """

    username = None  # type: ignore[assignment]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.AGENT)

    # The ClassVar follows django-stubs, which declares REQUIRED_FIELDS on
    # AbstractBaseUser as a class variable and USERNAME_FIELD as an instance one.
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects: ClassVar[UserManager] = UserManager()  # type: ignore[assignment]

    class Meta:
        ordering = ("first_name", "last_name", "email")
        indexes = [models.Index(fields=["role"])]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.get_full_name().strip() or self.email

    @property
    def is_supervisor(self) -> bool:
        return self.role == Role.SUPERVISOR
