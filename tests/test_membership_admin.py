"""The Membership inline on the tenant admin."""

import pytest
from django.contrib import admin
from django.test import Client

from accounts.models import User
from tenancy.models import Membership, Tenant

pytestmark = pytest.mark.django_db


def test_the_tenant_admin_edits_memberships_inline_with_a_raw_id_user() -> None:
    """TenantAdmin carries a Membership inline whose user is a raw id field."""
    inlines = admin.site._registry[Tenant].inlines
    assert [i.model for i in inlines] == [Membership]
    assert inlines[0].raw_id_fields == ("user",)


def test_the_tenant_change_page_lists_its_members(client: Client) -> None:
    """A superuser sees the member row on the tenant change page.

    Args:
        client: Test client.
    """
    root = User.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(root)
    tenant = Tenant.objects.create(name="Acme")
    Membership.objects.create(user=root, tenant=tenant, role=Membership.Role.OWNER)
    response = client.get(f"/admin/tenancy/tenant/{tenant.pk}/change/")
    assert response.status_code == 200
    assert b"memberships-0-role" in response.content
