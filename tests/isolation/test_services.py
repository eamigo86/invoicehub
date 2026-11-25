"""Billing services never act across tenants, whatever the principal holds."""

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from billing.models import (
    AuditEvent,
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceSeries,
    Product,
    ProductTaxRate,
    TaxRate,
)
from billing.services import (
    DraftLine,
    delete_customer,
    delete_draft,
    delete_product,
    delete_series,
    delete_tax_rate,
    issue_invoice,
    record_event,
    save_customer,
    save_draft,
    save_product,
    save_series,
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


def test_b_cannot_invoice_a_customer_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, a draft for A's customer is refused and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme"))
    with tenant_context(b.pk):
        with pytest.raises(ValidationError):
            save_draft(owner_of(b), Invoice(customer=customer), [])
        assert (Invoice.objects.count(), AuditEvent.objects.count()) == (0, 0)


def test_b_cannot_bill_a_product_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, a line for A's product is refused and no invoice is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        product = save_product(owner_of(a), Product(name="Widget", unit_price=1))
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt"))
        line = DraftLine("Widget", Decimal("1"), 1, product=product)
        with pytest.raises(ValidationError):
            save_draft(owner_of(b), Invoice(customer=customer), [line])
        assert (Invoice.objects.count(), InvoiceLine.objects.count()) == (0, 0)


def test_b_cannot_tax_a_line_with_a_rate_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, a foreign tax rate is refused and the existing draft keeps its lines.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        foreign = save_tax_rate(owner_of(a), TaxRate(name="Austin", rate_bp=825))
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt"))
        draft = save_draft(
            owner_of(b), Invoice(customer=customer), [DraftLine("Old", Decimal(1), 5)]
        )
        line = DraftLine("New", Decimal("1"), 1, tax_rates=(foreign,))
        with pytest.raises(ValidationError):
            save_draft(owner_of(b), draft, [line])
        assert list(draft.lines.values_list("description", flat=True)) == ["Old"]


def test_b_cannot_modify_a_draft_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, saving A's draft is refused, since B cannot see its customer, and A is untouched.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme"))
        draft = save_draft(
            owner_of(a), Invoice(customer=customer), [DraftLine("Old", Decimal(1), 5)]
        )
    with tenant_context(b.pk), pytest.raises(ValidationError):
        save_draft(owner_of(b), draft, [DraftLine("Hacked", Decimal(1), 1)])
    with tenant_context(a.pk):
        assert list(draft.lines.values_list("description", flat=True)) == ["Old"]


def test_b_cannot_delete_a_draft_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, deleting A's draft is a no-op, because Row-Level Security hides the row.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme"))
        draft = save_draft(owner_of(a), Invoice(customer=customer), [])
    with tenant_context(b.pk):
        delete_draft(owner_of(b), draft)
        assert AuditEvent.objects.count() == 0
    with tenant_context(a.pk):
        assert Invoice.objects.filter(pk=draft.pk).exists()


def test_a_principal_of_a_cannot_save_a_draft_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A holding invoices:write is refused under B and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt"))
        with pytest.raises(PermissionDenied):
            save_draft(owner_of(a), Invoice(customer=customer), [])
        assert Invoice.objects.count() == 0


def test_a_principal_of_a_cannot_delete_a_draft_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A cannot delete B's draft while B is current.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt"))
        draft = save_draft(owner_of(b), Invoice(customer=customer), [])
        with pytest.raises(PermissionDenied):
            delete_draft(owner_of(a), draft)
        assert Invoice.objects.filter(pk=draft.pk).exists()


def complete_issuer(*tenants: Tenant) -> None:
    """Fill in the issuer details that issuing an invoice requires.

    Args:
        *tenants: Tenants to complete.
    """
    for tenant in tenants:
        Tenant.objects.filter(pk=tenant.pk).update(
            legal_name=f"{tenant.name} Inc.",
            address_line1="1 Main St",
            city="Austin",
            state="TX",
            postal_code="78701",
        )


def test_b_cannot_issue_a_draft_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, issuing A's draft finds no row and A's draft stays a draft, unnumbered.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme"))
        draft = save_draft(
            owner_of(a), Invoice(customer=customer), [DraftLine("Old", Decimal(1), 5)]
        )
    with tenant_context(b.pk), pytest.raises(Invoice.DoesNotExist):
        issue_invoice(owner_of(b), draft)
    with tenant_context(b.pk):
        assert (Invoice.objects.count(), InvoiceSeries.objects.count()) == (0, 0)
    with tenant_context(a.pk):
        stored = Invoice.objects.get(pk=draft.pk)
        assert (stored.status, stored.number) == (Invoice.Status.DRAFT, None)
        assert not InvoiceSeries.objects.exists()


def test_a_principal_of_a_cannot_issue_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A is refused under B and B's draft is not numbered.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt"))
        draft = save_draft(
            owner_of(b), Invoice(customer=customer), [DraftLine("Old", Decimal(1), 5)]
        )
        with pytest.raises(PermissionDenied):
            issue_invoice(owner_of(a), draft)
        assert Invoice.objects.get().status == Invoice.Status.DRAFT
        assert not InvoiceSeries.objects.exists()


def test_the_series_counters_of_two_tenants_are_independent(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """B's issues never move A's series counter, and each tenant numbers from 1.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    numbers: dict[str, list[int | None]] = {}
    for tenant, count in ((a, 1), (b, 3)):
        with tenant_context(tenant.pk):
            customer = save_customer(owner_of(tenant), Customer(name="Client"))
            drafts = [
                save_draft(
                    owner_of(tenant), Invoice(customer=customer), [DraftLine("L", Decimal(1), 5)]
                )
                for _ in range(count)
            ]
            numbers[tenant.name] = [issue_invoice(owner_of(tenant), d).number for d in drafts]
    assert numbers == {"A": [1], "B": [1, 2, 3]}
    with tenant_context(a.pk):
        assert InvoiceSeries.objects.get().next_number == 2
    with tenant_context(b.pk):
        assert InvoiceSeries.objects.get().next_number == 4


def test_b_cannot_edit_the_series_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's series is not found and keeps its prefix and counter.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        series = save_series(owner_of(a), InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
    series.prefix = "HACK-"
    with tenant_context(b.pk), pytest.raises(InvoiceSeries.DoesNotExist):
        save_series(owner_of(b), series)
    with tenant_context(a.pk):
        stored = InvoiceSeries.objects.get()
        assert (stored.prefix, stored.next_number) == ("INV-", 1)


def test_a_principal_of_a_cannot_save_a_series_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A holding settings:manage is refused under B and nothing is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        with pytest.raises(PermissionDenied):
            save_series(owner_of(a), InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
        assert not InvoiceSeries.objects.exists()


def test_a_principal_of_a_cannot_delete_a_series_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A holding settings:manage cannot delete B's series while B is current.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk):
        series = save_series(owner_of(b), InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
        with pytest.raises(PermissionDenied):
            delete_series(owner_of(a), series)
        assert InvoiceSeries.objects.filter(pk=series.pk).exists()


def test_b_cannot_delete_the_series_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, deleting A's series is a no-op, because Row-Level Security hides the row.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        series = save_series(owner_of(a), InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
    pk = series.pk
    with tenant_context(b.pk):
        delete_series(owner_of(b), series)
    with tenant_context(a.pk):
        assert InvoiceSeries.objects.filter(pk=pk).exists()
