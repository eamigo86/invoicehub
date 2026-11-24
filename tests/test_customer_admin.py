"""Customer admin: hidden without a tenant, writes routed through the services.

These tests use real transactions because the tenant reaches PostgreSQL only when a request's
outermost transaction begins.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.admin.models import LogEntry
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


@pytest.mark.parametrize("path", ["", "add/", "{pk}/change/", "{pk}/delete/", "{pk}/history/"])
def test_every_customer_admin_url_is_403_without_a_tenant(
    client: Client, tenant: Tenant, path: str
) -> None:
    """With no active tenant each customer admin URL answers 403, never a crash or a page.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
        path: URL suffix under the customer admin, with a placeholder for a primary key.
    """
    with tenant_context(tenant.pk):
        pk = Customer.objects.get(name="Acme").pk
    client.raise_request_exception = False
    login(client, None)
    assert client.get(LIST_URL + path.format(pk=pk)).status_code == 403


def test_bulk_delete_without_a_tenant_is_403_and_deletes_nothing(
    client: Client, tenant: Tenant
) -> None:
    """The delete-selected action cannot be used while no tenant is active.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
    """
    with tenant_context(tenant.pk):
        pks = [str(c.pk) for c in Customer.objects.all()]
    client.raise_request_exception = False
    login(client, None)
    data = {"action": "delete_selected", "_selected_action": pks, "post": "yes"}
    assert client.post(LIST_URL, data).status_code == 403
    with tenant_context(tenant.pk):
        assert Customer.objects.count() == 2


def test_customer_admin_is_not_listed_in_the_index_without_a_tenant(
    client: Client, tenant: Tenant
) -> None:
    """The admin index does not offer the customer admin while no tenant is active.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
    """
    login(client, None)
    assert "/admin/billing/customer/" not in client.get("/admin/").content.decode()


def test_admin_writes_leave_no_log_entries(client: Client, tenant: Tenant) -> None:
    """Adding, changing and deleting through the admin never writes a LogEntry.

    Object representations in the admin log would leak across tenants, so audit logging is
    left to the services.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning customers.
    """
    with tenant_context(tenant.pk):
        acme, bolt = Customer.objects.get(name="Acme"), Customer.objects.get(name="Bolt")
    login(client, tenant.pk)
    client.post(f"{LIST_URL}add/", {"name": "Cora", "tax_id": "", "email": ""})
    client.post(f"{LIST_URL}{acme.pk}/change/", {"name": "Acme 2", "tax_id": "", "email": ""})
    client.post(f"{LIST_URL}{acme.pk}/delete/", {"post": "yes"})
    data = {"action": "delete_selected", "_selected_action": [str(bolt.pk)], "post": "yes"}
    client.post(LIST_URL, data)
    with tenant_context(tenant.pk):
        assert sorted(Customer.objects.values_list("name", flat=True)) == ["Cora"]
    assert LogEntry.objects.count() == 0


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
