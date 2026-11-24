"""The session tenant only takes effect for authenticated members of that tenant."""

import pytest
from django.test import Client

from accounts.models import User
from tenancy.models import Membership, Tenant

pytestmark = [
    pytest.mark.isolation,
    pytest.mark.django_db(transaction=True),
    pytest.mark.urls("tests.urls_tenant_probe"),
]


def probe_with(client: Client, tenant: Tenant) -> str:
    """Store a tenant in the session and read what the probe view observes.

    Args:
        client: Test client, logged in or not.
        tenant: Tenant written to the session.

    Returns:
        The tenant setting seen by the view's transaction.
    """
    session = client.session
    session["tenant_id"] = str(tenant.pk)
    session.save()
    return client.get("/probe/").content.decode()


def test_non_member_with_a_forged_session_tenant_gets_no_tenant(client: Client) -> None:
    """A logged-in user outside the tenant sees no tenant and the key is dropped.

    Args:
        client: Django test client fixture.
    """
    client.force_login(User.objects.create_user("eve", "eve@example.com", "pw"))
    assert probe_with(client, Tenant.objects.create(name="A")) == ""
    assert "tenant_id" not in client.session


def test_anonymous_request_with_a_session_tenant_gets_no_tenant(client: Client) -> None:
    """An anonymous session naming a tenant is ignored.

    Args:
        client: Django test client fixture.
    """
    assert probe_with(client, Tenant.objects.create(name="A")) == ""
    assert "tenant_id" not in client.session


def test_member_of_a_cannot_enter_b_through_the_session(client: Client) -> None:
    """A member of A gets A but not B, whichever one the session names.

    Args:
        client: Django test client fixture.
    """
    a, b = Tenant.objects.create(name="A"), Tenant.objects.create(name="B")
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    Membership.objects.create(user=user, tenant=a, role=Membership.Role.VIEWER)
    client.force_login(user)
    assert probe_with(client, a) == str(a.pk)
    assert probe_with(client, b) == ""
    assert "tenant_id" not in client.session
