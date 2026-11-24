"""Customer services, the single write path for views, the admin and later the API."""

import pytest

from billing.models import Customer
from billing.services import delete_customer, save_customer
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def test_save_customer_persists_under_the_current_tenant() -> None:
    """A new customer is stored for the active tenant and returned."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        saved = save_customer(Customer(name="Acme"))
        assert saved.pk is not None
        assert Customer.objects.get(pk=saved.pk).tenant_id == tenant.pk


def test_save_customer_updates_an_existing_row() -> None:
    """Saving a changed customer updates it instead of creating another."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        customer = save_customer(Customer(name="Acme"))
        customer.name = "Acme Ltd"
        save_customer(customer)
        assert list(Customer.objects.values_list("name", flat=True)) == ["Acme Ltd"]


def test_delete_customer_removes_the_row() -> None:
    """Deleting a customer removes it from the tenant's data."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        customer = save_customer(Customer(name="Acme"))
        delete_customer(customer)
        assert Customer.objects.count() == 0
