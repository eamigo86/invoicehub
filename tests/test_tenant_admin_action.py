"""Provisional Activate action on the tenant admin: a confirmation page and a POST."""

import uuid

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from accounts.models import User
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def activate_url(tenant_id: object) -> str:
    """Build the Activate URL of a tenant.

    Args:
        tenant_id: Identifier placed in the URL, valid or not.

    Returns:
        The admin path of the action.
    """
    return f"/admin/tenancy/tenant/{tenant_id}/activate/"


def superuser(client: Client) -> None:
    """Log a superuser in.

    Args:
        client: Test client to log in.
    """
    client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))


def staff(client: Client) -> None:
    """Log in a staff user who may view tenants but is not a superuser.

    Args:
        client: Test client to log in.
    """
    user = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_tenant"))
    client.force_login(user)


def test_get_shows_a_confirmation_and_changes_nothing(client: Client) -> None:
    """GET renders the tenant name and a CSRF protected form, and leaves the session alone.

    Args:
        client: Django test client fixture.
    """
    tenant = Tenant.objects.create(name="Acme Holdings")
    superuser(client)
    response = client.get(activate_url(tenant.pk))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Acme Holdings" in body
    assert "csrfmiddlewaretoken" in body
    assert "tenant_id" not in client.session


def test_post_without_a_csrf_token_is_rejected() -> None:
    """A POST that lacks the CSRF token is refused and activates nothing."""
    tenant = Tenant.objects.create(name="A")
    client = Client(enforce_csrf_checks=True)
    superuser(client)
    assert client.post(activate_url(tenant.pk)).status_code == 403
    assert "tenant_id" not in client.session


def test_post_as_superuser_activates_the_tenant(client: Client) -> None:
    """POST stores the tenant in the session and redirects to the changelist.

    Args:
        client: Django test client fixture.
    """
    tenant = Tenant.objects.create(name="A")
    superuser(client)
    response = client.post(activate_url(tenant.pk))
    assert response.status_code == 302
    assert response["Location"] == "/admin/tenancy/tenant/"
    assert client.session["tenant_id"] == str(tenant.pk)


@pytest.mark.parametrize("method", ["get", "post"])
def test_staff_without_superuser_is_refused(client: Client, method: str) -> None:
    """A non-superuser staff user is refused on both GET and POST.

    Args:
        client: Django test client fixture.
        method: HTTP method to use.
    """
    tenant = Tenant.objects.create(name="A")
    staff(client)
    assert getattr(client, method)(activate_url(tenant.pk)).status_code == 403
    assert "tenant_id" not in client.session


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("bad_id", ["not-a-uuid", str(uuid.uuid4())], ids=["invalid", "unknown"])
def test_invalid_or_unknown_tenant_is_a_404(client: Client, method: str, bad_id: str) -> None:
    """An id that is malformed or names no tenant answers 404 and changes nothing.

    Args:
        client: Django test client fixture.
        method: HTTP method to use.
        bad_id: Identifier placed in the URL.
    """
    superuser(client)
    assert getattr(client, method)(activate_url(bad_id)).status_code == 404
    assert "tenant_id" not in client.session
