"""Tenancy models."""

import uuid
import zoneinfo
from typing import Any

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from core.models import Currency, PostalAddress
from tenancy.context import TenantMismatch, require_current_tenant_id


def validate_timezone(value: str) -> None:
    """Check that a value is an IANA timezone name known to zoneinfo.

    Args:
        value: Timezone name to check.

    Raises:
        ValidationError: When the name is not an available timezone.
    """
    if value not in zoneinfo.available_timezones():
        raise ValidationError("Unknown timezone: %(value)s.", params={"value": value})


class Tenant(PostalAddress):
    """A customer organisation that owns tenant-scoped data.

    This table is the tenant registry, not tenant-owned data, so it deliberately has no
    Row-Level Security: it must stay readable to resolve and list tenants before any tenant is
    active.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    legal_name = models.CharField(max_length=200, blank=True)
    ein = models.CharField(
        max_length=10,
        blank=True,
        validators=[RegexValidator(r"^\d{2}-\d{7}$", "Enter an EIN as NN-NNNNNNN.")],
    )
    default_currency = models.CharField(max_length=3, choices=Currency, default=Currency.USD)
    timezone = models.CharField(
        max_length=64, default="America/New_York", validators=[validate_timezone]
    )

    def __str__(self) -> str:
        """Return the tenant name.

        Returns:
            The display name of the tenant.
        """
        return self.name


class Membership(models.Model):
    """The role a user holds in a tenant.

    Memberships are looked up before any tenant is active, to decide which tenants a user may
    enter, so like the user table this one is deliberately not a TenantModel and has no
    Row-Level Security. Callers always filter by user.
    """

    class Role(models.TextChoices):
        """Roles a member can hold, each mapped to permissions in tenancy.permissions."""

        OWNER = "owner"
        ADMIN = "admin"
        ACCOUNTANT = "accountant"
        VIEWER = "viewer"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="membership_user_tenant_uniq")
        ]

    def __str__(self) -> str:
        """Describe the membership by user, tenant and role.

        Returns:
            A label such as ann @ Acme (viewer).
        """
        return f"{self.user} @ {self.tenant} ({self.role})"


class ApiToken(models.Model):
    """A revocable credential that lets a program act inside one tenant.

    Tokens are looked up by hash before any tenant is active, so like Membership this table is
    deliberately not a TenantModel and has no Row-Level Security. Only the sha256 hex of the
    token is stored, never the token itself. Callers always filter by tenant or by token hash.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=200)
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = ArrayField(models.CharField(max_length=50))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    allow_introspection = models.BooleanField(default=False)

    def __str__(self) -> str:
        """Return the token name.

        Returns:
            The display name of the token.
        """
        return self.name


class TenantManager(models.Manager[Any]):
    """Manager whose querysets only ever see the current tenant's rows.

    This is the application-level second barrier; Row-Level Security is the real one.
    """

    def get_queryset(self) -> models.QuerySet[Any]:
        """Return the queryset restricted to the current tenant.

        Returns:
            A queryset filtered by the active tenant. TenantNotSet is raised when no tenant is
            active.
        """
        return super().get_queryset().filter(tenant_id=require_current_tenant_id())


class TenantModel(models.Model):
    """Abstract base of every business model owned by a tenant.

    Rows are protected twice: the default manager filters by the current tenant, and save
    refuses to write a row that belongs to another tenant.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, editable=False, related_name="+")

    tenant_id: uuid.UUID | None

    objects = TenantManager()

    class Meta:
        """Model options."""

        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save the row on behalf of the current tenant.

        A new row without a tenant receives the active one. TenantNotSet is raised when no
        tenant is active.

        Args:
            *args: Positional arguments forwarded to Model.save.
            **kwargs: Keyword arguments forwarded to Model.save.

        Raises:
            TenantMismatch: When the row belongs to a tenant other than the active one.
        """
        current = require_current_tenant_id()
        if self.tenant_id is None:
            self.tenant_id = current
        elif str(self.tenant_id) != str(current):
            raise TenantMismatch(f"row belongs to tenant {self.tenant_id}, active is {current}")
        super().save(*args, **kwargs)
