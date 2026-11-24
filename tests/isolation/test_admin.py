"""The customer admin only ever shows and changes the active tenant's rows.

Real transactions are required: the tenant reaches PostgreSQL when a request's outermost
transaction begins.
"""

import uuid

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from accounts.models import User
from billing.models import Customer
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = [pytest.mark.isolation, pytest.mark.django_db(transaction=True)]

LIST_URL = "/admin/billing/customer/"


class Setup:
    """Two tenants with one named customer each, and a client logged in as a superuser.

    Attributes:
        a: Tenant A.
        b: Tenant B.
        b_pk: Primary key of B's customer.
        client: Logged in test client with no session tenant yet.
    """

    def __init__(self, client: Client) -> None:
        """Create the data and log in.

        Args:
            client: Test client to log in.
        """
        self.a = Tenant.objects.create(name="A")
        self.b = Tenant.objects.create(name="B")
        with tenant_context(self.a.pk):
            Customer.objects.create(name="alpha-visible")
        with tenant_context(self.b.pk):
            self.b_pk = Customer.objects.create(name="bravo-secret").pk
        self.client = client
        client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))

    def activate(self, tenant_id: uuid.UUID) -> None:
        """Put a tenant in the session.

        Args:
            tenant_id: Tenant to make active.
        """
        session = self.client.session
        session["tenant_id"] = str(tenant_id)
        session.save()

    def b_name(self) -> str:
        """Read B's customer name directly under B.

        Returns:
            The stored name.
        """
        with tenant_context(self.b.pk):
            name: str = Customer.objects.get(pk=self.b_pk).name
        return name


@pytest.fixture
def setup(client: Client) -> Setup:
    """Create the admin scenario.

    Args:
        client: Django test client fixture.

    Returns:
        The prepared scenario with tenant A active.
    """
    scenario = Setup(client)
    scenario.activate(scenario.a.pk)
    return scenario


def test_changelist_shows_only_the_active_tenants_rows(setup: Setup) -> None:
    """A's customer is listed and B's is not.

    Args:
        setup: Admin scenario with tenant A active.
    """
    body = setup.client.get(LIST_URL).content.decode()
    assert "alpha-visible" in body
    assert "bravo-secret" not in body


@pytest.mark.parametrize("suffix", ["change/", "delete/"])
def test_foreign_change_and_delete_views_do_not_render(setup: Setup, suffix: str) -> None:
    """B's change and delete pages are not reachable and never show B's data.

    Args:
        setup: Admin scenario with tenant A active.
        suffix: Final part of the admin URL.
    """
    url = f"{LIST_URL}{setup.b_pk}/{suffix}"
    response = setup.client.get(url)
    assert response.status_code == 302
    assert "bravo-secret" not in setup.client.get(url, follow=True).content.decode()


def test_posting_to_a_foreign_change_view_does_not_modify_it(setup: Setup) -> None:
    """A POST aimed at B's customer leaves it untouched, and so does a delete POST.

    Args:
        setup: Admin scenario with tenant A active.
    """
    setup.client.post(
        f"{LIST_URL}{setup.b_pk}/change/", {"name": "hijacked", "tax_id": "", "email": ""}
    )
    setup.client.post(f"{LIST_URL}{setup.b_pk}/delete/", {"post": "yes"})
    assert setup.b_name() == "bravo-secret"


def test_admin_is_hidden_without_a_session_tenant(client: Client) -> None:
    """With no tenant in the session the customer admin is not accessible.

    Args:
        client: Django test client fixture.
    """
    scenario = Setup(client)
    assert scenario.client.get(LIST_URL).status_code == 403


def test_activate_action_selects_the_tenant_for_superusers(client: Client) -> None:
    """A superuser activates tenant A and then sees only A's customers.

    Args:
        client: Django test client fixture.
    """
    scenario = Setup(client)
    client.post(f"/admin/tenancy/tenant/{scenario.a.pk}/activate/")
    body = client.get(LIST_URL).content.decode()
    assert "alpha-visible" in body
    assert "bravo-secret" not in body


def test_activate_action_is_refused_for_non_superusers(client: Client) -> None:
    """A staff user without superuser rights cannot select a tenant.

    Args:
        client: Django test client fixture.
    """
    scenario = Setup(client)
    staff = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="view_tenant"))
    client.force_login(staff)
    assert client.post(f"/admin/tenancy/tenant/{scenario.a.pk}/activate/").status_code == 403
    assert "tenant_id" not in client.session
