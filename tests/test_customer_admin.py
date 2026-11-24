"""Customer admin: hidden without a tenant, writes routed through the services.

These tests use real transactions because the tenant reaches PostgreSQL only when a request's
outermost transaction begins.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client

import billing.admin
from accounts.models import User
from billing.models import Customer
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

LIST_URL = "/admin/billing/customer/"


def login(client: Client, tenant_id: uuid.UUID | None) -> None:
    """Log a superuser in and store the active tenant in the session.

    Args:
        client: Test client to prepare.
        tenant_id: Tenant to put in the session, or None for none.
    """
    user = User.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(user)
    if tenant_id is not None:
        session = client.session
        session["tenant_id"] = str(tenant_id)
        session.save()


@pytest.fixture
def tenant() -> Tenant:
    """Create a tenant with two customers.

    Returns:
        The tenant.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        Customer.objects.create(name="Acme")
        Customer.objects.create(name="Bolt")
    return tenant


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record the service calls made by the admin while still running them.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A list filled with pairs of service name and customer name.
    """
    log: list[tuple[str, str]] = []

    def wrap(name: str, func: Callable[[Customer], Any]) -> Callable[[Customer], Any]:
        """Wrap a service so that its calls are recorded.

        Args:
            name: Label stored in the log.
            func: Service to call through.

        Returns:
            The recording wrapper.
        """

        def inner(customer: Customer) -> Any:
            """Record and delegate.

            Args:
                customer: Customer passed to the service.

            Returns:
                Whatever the service returns.
            """
            log.append((name, customer.name))
            return func(customer)

        return inner

    monkeypatch.setattr(billing.admin, "save_customer", wrap("save", billing.admin.save_customer))  # type: ignore[attr-defined]
    monkeypatch.setattr(
        billing.admin,
        "delete_customer",
        wrap("delete", billing.admin.delete_customer),  # type: ignore[attr-defined]
    )
    return log


def test_customer_admin_is_hidden_without_a_tenant(client: Client, tenant: Tenant) -> None:
    """With no active tenant the customer admin is neither listed nor reachable.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
    """
    client.raise_request_exception = False
    login(client, None)
    assert client.get(LIST_URL).status_code == 403
    assert client.get("/admin/billing/customer/add/").status_code == 403
    assert "/admin/billing/customer/" not in client.get("/admin/").content.decode()


def test_customer_admin_lists_the_active_tenants_customers(client: Client, tenant: Tenant) -> None:
    """With an active tenant the changelist works and is listed in the index.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
    """
    login(client, tenant.pk)
    response = client.get(LIST_URL)
    assert response.status_code == 200
    assert "Acme" in response.content.decode()
    assert "/admin/billing/customer/" in client.get("/admin/").content.decode()


def test_add_goes_through_the_save_service(
    client: Client, tenant: Tenant, calls: list[tuple[str, str]]
) -> None:
    """Adding a customer in the admin calls save_customer.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
        calls: Recorded service calls.
    """
    login(client, tenant.pk)
    client.post("/admin/billing/customer/add/", {"name": "Cora", "tax_id": "", "email": ""})
    assert calls == [("save", "Cora")]
    with tenant_context(tenant.pk):
        assert Customer.objects.filter(name="Cora").exists()


def test_delete_goes_through_the_delete_service(
    client: Client, tenant: Tenant, calls: list[tuple[str, str]]
) -> None:
    """Deleting one customer in the admin calls delete_customer.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
        calls: Recorded service calls.
    """
    with tenant_context(tenant.pk):
        pk = Customer.objects.get(name="Acme").pk
    login(client, tenant.pk)
    client.post(f"/admin/billing/customer/{pk}/delete/", {"post": "yes"})
    assert calls == [("delete", "Acme")]


def test_bulk_delete_goes_through_the_delete_service(
    client: Client, tenant: Tenant, calls: list[tuple[str, str]]
) -> None:
    """The delete-selected action calls delete_customer for every row.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
        calls: Recorded service calls.
    """
    with tenant_context(tenant.pk):
        pks = [str(c.pk) for c in Customer.objects.all()]
    login(client, tenant.pk)
    client.post(LIST_URL, {"action": "delete_selected", "_selected_action": pks, "post": "yes"})
    assert sorted(calls) == [("delete", "Acme"), ("delete", "Bolt")]
    with tenant_context(tenant.pk):
        assert Customer.objects.count() == 0
