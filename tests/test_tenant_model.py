"""Tenant registry model and its admin page."""

import uuid

import pytest
from django.test import Client

from accounts.models import User
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def test_tenant_has_uuid_pk_and_name_str() -> None:
    """A tenant gets a generated UUID primary key and prints as its name."""
    tenant = Tenant.objects.create(name="Acme Ltd")
    assert isinstance(tenant.pk, uuid.UUID)
    assert tenant.pk.version == 4
    assert str(tenant) == "Acme Ltd"


def test_tenant_pk_is_not_editable_and_created_at_is_set() -> None:
    """The primary key is not editable and the creation time is filled automatically."""
    tenant = Tenant.objects.create(name="Acme Ltd")
    assert Tenant._meta.pk is not None
    assert Tenant._meta.pk.editable is False
    assert tenant.created_at is not None


def test_superuser_can_open_tenant_changelist(client: Client) -> None:
    """A superuser sees the tenant changelist in the admin.

    Args:
        client: Django test client fixture.
    """
    admin_user = User.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(admin_user)
    response = client.get("/admin/tenancy/tenant/")
    assert response.status_code == 200
