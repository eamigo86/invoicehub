"""Billing services never act across tenants, whatever the principal holds."""

import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from billing.models import AuditEvent, Customer, Product, ProductTaxRate, TaxRate
from billing.services import (
    delete_customer,
    delete_product,
    delete_tax_rate,
    record_event,
    save_customer,
    save_product,
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


def test_b_cannot_link_a_tax_rate_of_a_to_a_product(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, a product linked to A's tax rate is refused and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        rate = save_tax_rate(owner_of(a), TaxRate(name="Austin", rate_bp=825))
    with tenant_context(b.pk):
        with pytest.raises(ValidationError):
            save_product(owner_of(b), Product(name="Widget", unit_price=1), [rate])
        assert (Product.objects.count(), ProductTaxRate.objects.count()) == (0, 0)


def test_a_refused_foreign_tax_rate_leaves_the_existing_links_alone(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """Replacing taxes with a foreign rate fails as a whole and keeps the previous links.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        foreign = save_tax_rate(owner_of(a), TaxRate(name="Austin", rate_bp=825))
    with tenant_context(b.pk):
        own = save_tax_rate(owner_of(b), TaxRate(name="Dallas", rate_bp=100))
        product = save_product(owner_of(b), Product(name="Widget", unit_price=1), [own])
        with pytest.raises(ValidationError):
            save_product(owner_of(b), product, [foreign])
        assert list(product.default_taxes.values_list("name", flat=True)) == ["Dallas"]


def test_b_cannot_modify_a_product_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, saving A's product is refused and A's row keeps its name.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        product = save_product(owner_of(a), Product(name="Widget", unit_price=1))
    product.name = "Hacked"
    with tenant_context(b.pk), pytest.raises(TenantMismatch):
        save_product(owner_of(b), product)
    with tenant_context(a.pk):
        assert Product.objects.get(pk=product.pk).name == "Widget"


def test_b_cannot_delete_a_product_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, deleting A's product is a no-op, because Row-Level Security hides the row.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        product = save_product(owner_of(a), Product(name="Widget", unit_price=1))
    pk = product.pk
    with tenant_context(b.pk):
        delete_product(owner_of(b), product)
    with tenant_context(a.pk):
        assert Product.objects.filter(pk=pk).exists()


def test_a_principal_of_a_cannot_save_a_product_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A holding invoices:write is refused under B and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        with pytest.raises(PermissionDenied):
            save_product(owner_of(a), Product(name="Widget", unit_price=1))
        assert Product.objects.count() == 0


def test_a_principal_of_a_cannot_delete_a_product_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A cannot delete B's product while B is current.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        product = save_product(owner_of(b), Product(name="Widget", unit_price=1))
        with pytest.raises(PermissionDenied):
            delete_product(owner_of(a), product)
        assert Product.objects.filter(pk=product.pk).exists()


def test_a_principal_of_a_cannot_record_an_event_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A is refused under B and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        with pytest.raises(PermissionDenied):
            record_event(owner_of(a), "invoice.issued", "invoice", uuid.uuid4())
        assert AuditEvent.objects.count() == 0


def test_the_events_of_a_are_invisible_under_b(tenants: tuple[Tenant, Tenant]) -> None:
    """An event recorded for A is never listed, counted or fetched under B.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        event = record_event(owner_of(a), "invoice.issued", "invoice", uuid.uuid4())
    with tenant_context(b.pk):
        assert AuditEvent.objects.count() == 0
        assert not AuditEvent.objects.filter(pk=event.pk).exists()
        assert not AuditEvent._base_manager.filter(pk=event.pk).exists()
    with tenant_context(a.pk):
        assert AuditEvent.objects.get().pk == event.pk
