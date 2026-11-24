"""Tenancy models."""

import uuid

from django.db import models


class Tenant(models.Model):
    """A customer organisation that owns tenant-scoped data.

    This table is the tenant registry, not tenant-owned data, so it deliberately has no
    Row-Level Security: it must stay readable to resolve and list tenants before any tenant is
    active.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return the tenant name.

        Returns:
            The display name of the tenant.
        """
        return self.name
