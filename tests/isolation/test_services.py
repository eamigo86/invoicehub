"""Billing services never act across tenants, whatever the principal holds."""

import pytest
from django.core.exceptions import PermissionDenied

from billing.models import Customer, TaxRate
from billing.services import (
    delete_customer,
    delete_tax_rate,
    save_customer,
    save_tax_rate,
    update_issuer,
)
from tenancy.context import TenantMismatch, tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = [pytest.mark.isolation, pytest.mark.django_db]


def owner_of(tenant: Tenant) -> Principal:
    """Build a principal holding every permission in a tenant.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, frozenset(Permission))


@pytest.fixture
def tenants() -> tuple[Tenant, Tenant]:
    """Create two tenants.

    Returns:
        Tenants A and B.
    """
    return Tenant.objects.create(name="A"), Tenant.objects.create(name="B")


def test_b_cannot_modify_a_customer_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, saving A's customer is refused and A's row keeps its name.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme"))
    customer.name = "Hacked"
    with tenant_context(b.pk), pytest.raises(TenantMismatch):
        save_customer(owner_of(b), customer)
    with tenant_context(a.pk):
        assert Customer.objects.get(pk=customer.pk).name == "Acme"


def test_b_cannot_delete_a_customer_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, deleting A's customer is a no-op, because Row-Level Security hides the row.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme"))
    pk = customer.pk
    with tenant_context(b.pk):
        delete_customer(owner_of(b), customer)
    with tenant_context(a.pk):
        assert Customer.objects.filter(pk=pk).exists()


def test_a_principal_of_a_cannot_save_while_b_is_current(tenants: tuple[Tenant, Tenant]) -> None:
    """A principal of A holding invoices:write is refused under B and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        with pytest.raises(PermissionDenied):
            save_customer(owner_of(a), Customer(name="Acme"))
        assert Customer.objects.count() == 0


def test_a_principal_of_a_cannot_delete_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A holding invoices:write cannot delete B's customer while B is current.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt"))
        with pytest.raises(PermissionDenied):
            delete_customer(owner_of(a), customer)
        assert Customer.objects.filter(pk=customer.pk).exists()


def test_a_principal_of_a_cannot_update_the_issuer_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A owner of A is refused under B and B's issuer data is unchanged.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    b.legal_name = "Hijacked"
    with tenant_context(b.pk), pytest.raises(PermissionDenied):
        update_issuer(owner_of(a), b)
    assert Tenant.objects.get(pk=b.pk).legal_name == ""


def test_an_owner_of_b_cannot_update_the_issuer_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """A owner of B who passes tenant A's instance is refused and A is unchanged.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    a.legal_name = "Hijacked"
    with tenant_context(b.pk), pytest.raises(PermissionDenied):
        update_issuer(owner_of(b), a)
    assert Tenant.objects.get(pk=a.pk).legal_name == ""


def test_b_cannot_modify_a_tax_rate_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, saving A's tax rate is refused and A's row keeps its rate.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        rate = save_tax_rate(owner_of(a), TaxRate(name="Austin", rate_bp=825))
    rate.rate_bp = 1
    with tenant_context(b.pk), pytest.raises(TenantMismatch):
        save_tax_rate(owner_of(b), rate)
    with tenant_context(a.pk):
        assert TaxRate.objects.get(pk=rate.pk).rate_bp == 825


def test_b_cannot_delete_a_tax_rate_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, deleting A's tax rate is a no-op, because Row-Level Security hides the row.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        rate = save_tax_rate(owner_of(a), TaxRate(name="Austin", rate_bp=825))
    pk = rate.pk
    with tenant_context(b.pk):
        delete_tax_rate(owner_of(b), rate)
    with tenant_context(a.pk):
        assert TaxRate.objects.filter(pk=pk).exists()


def test_a_principal_of_a_cannot_save_a_tax_rate_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A holding settings:manage is refused under B and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        with pytest.raises(PermissionDenied):
            save_tax_rate(owner_of(a), TaxRate(name="Austin", rate_bp=825))
        assert TaxRate.objects.count() == 0


def test_a_principal_of_a_cannot_delete_a_tax_rate_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A cannot delete B's tax rate while B is current.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        rate = save_tax_rate(owner_of(b), TaxRate(name="Austin", rate_bp=825))
        with pytest.raises(PermissionDenied):
            delete_tax_rate(owner_of(a), rate)
        assert TaxRate.objects.filter(pk=rate.pk).exists()
