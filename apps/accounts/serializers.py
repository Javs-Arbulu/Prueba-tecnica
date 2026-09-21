from __future__ import annotations

from typing import Any

from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import User


class UserSerializer(serializers.ModelSerializer):
    """The only representation of an internal user the API ever returns.

    Note what is absent: ``id``, ``password``, ``is_superuser``, ``last_login``.
    """

    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("public_id", "email", "first_name", "last_name", "display_name", "role")
        read_only_fields = fields


class SupportTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Adds the role to the token and the user object to the login response.

    The client needs the role to render the right controls; making it do a second
    round trip to ``/me/`` right after logging in is gratuitous.
    """

    @classmethod
    def get_token(cls, user: User):  # type: ignore[override]
        token = super().get_token(user)
        # The subject claim is already the public id (see SIMPLE_JWT); the role
        # is added because the client needs it to render the right controls.
        token["role"] = user.role
        return token

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return {**super().validate(attrs), "user": UserSerializer(self.user).data}
