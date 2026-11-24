"""Provisional Activate action on the tenant admin."""

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from accounts.models import User
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def test_superuser_activates_a_tenant(client: Client) -> None:
    """Activate stores the tenant in the session and redirects back to the changelist.

    Args:
        client: Django test client fixture.
    """
    tenant = Tenant.objects.create(name="A")
    client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))
    response = client.get(f"/admin/tenancy/tenant/{tenant.pk}/activate/")
    assert response.status_code == 302
    assert response["Location"] == "/admin/tenancy/tenant/"
    assert client.session["tenant_id"] == str(tenant.pk)


def test_staff_without_superuser_is_refused(client: Client) -> None:
    """A staff user that is not a superuser cannot activate a tenant.

    Args:
        client: Django test client fixture.
    """
    tenant = Tenant.objects.create(name="A")
    staff = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="view_tenant"))
    client.force_login(staff)
    response = client.get(f"/admin/tenancy/tenant/{tenant.pk}/activate/")
    assert response.status_code == 403
    assert "tenant_id" not in client.session
