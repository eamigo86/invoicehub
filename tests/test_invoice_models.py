"""Invoice, line and line tax models: defaults, validation, ordering and delete behavior."""

from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory

from billing.models import Customer, Invoice, InvoiceLine, InvoiceLineTax, Product, TaxRate
from core.models import Currency
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant and make it the active one for the test.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        yield tenant


def make_invoice() -> Invoice:
    """Create a draft invoice for a new customer in the active tenant.

    Returns:
        The saved invoice.
    """
    customer = Customer.objects.create(name="Acme")
    invoice: Invoice = Invoice.objects.create(customer=customer, currency=Currency.USD)
    return invoice


def make_line(invoice: Invoice, position: int = 1, **overrides: Any) -> InvoiceLine:
    """Build a valid line of an invoice, unsaved.

    Args:
        invoice: Invoice the line belongs to.
        position: Position of the line.
        **overrides: Field values that replace the defaults.

    Returns:
        The unsaved line.
    """
    values: dict[str, Any] = {
        "invoice": invoice,
        "position": position,
        "description": "Widget",
        "quantity": Decimal("2"),
        "unit_price": 1000,
        "discount_bp": 0,
        "net": 2000,
        "total": 2000,
    }
    values.update(overrides)
    return InvoiceLine(tenant_id=invoice.tenant_id, **values)


def test_a_new_invoice_is_a_draft_invoice_with_zero_totals(tenant: Tenant) -> None:
    """Kind, status and amounts have their documented defaults, and no dates are set.

    Args:
        tenant: Active tenant.
    """
    invoice = make_invoice()
    stored = Invoice.objects.get(pk=invoice.pk)
    assert (stored.kind, stored.status) == (Invoice.Kind.INVOICE, Invoice.Status.DRAFT)
    assert (stored.subtotal, stored.tax_total, stored.total) == (0, 0, 0)
    assert (stored.issue_date, stored.due_date, stored.notes) == (None, None, "")
    assert stored.tenant_id == tenant.pk


def test_overdue_is_not_a_stored_status() -> None:
    """Overdue is derived later, so it is not among the stored statuses."""
    assert "overdue" not in Invoice.Status.values
    assert Invoice.Status.values == [
        "draft",
        "issued",
        "sent",
        "partially_paid",
        "paid",
        "voided",
    ]


def test_an_invoice_with_invoices_cannot_lose_its_customer(tenant: Tenant) -> None:
    """Deleting a customer that has an invoice is refused.

    Args:
        tenant: Active tenant.
    """
    invoice = make_invoice()
    with pytest.raises(ProtectedError):
        invoice.customer.delete()


def test_lines_come_back_ordered_by_position(tenant: Tenant) -> None:
    """The lines of an invoice are listed by position, not by insertion.

    Args:
        tenant: Active tenant.
    """
    invoice = make_invoice()
    for position in (3, 1, 2):
        make_line(invoice, position).save()
    assert [line.position for line in invoice.lines.all()] == [1, 2, 3]


def test_two_lines_cannot_share_a_position(tenant: Tenant) -> None:
    """The position is unique within an invoice.

    Args:
        tenant: Active tenant.
    """
    invoice = make_invoice()
    make_line(invoice, 1).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_line(invoice, 1).save()


@pytest.mark.parametrize(
    "overrides",
    [{"discount_bp": 10001}, {"quantity": Decimal("-1")}, {"unit_price": -1}],
)
def test_a_line_out_of_range_is_invalid(tenant: Tenant, overrides: dict[str, Any]) -> None:
    """A discount above 100 percent, a negative quantity or a negative price is refused.

    Args:
        tenant: Active tenant.
        overrides: Out of range field value.
    """
    line = make_line(make_invoice(), **overrides)
    with pytest.raises(ValidationError):
        line.full_clean()


def test_a_line_tax_keeps_its_snapshot_when_the_rate_is_deleted(tenant: Tenant) -> None:
    """Deleting the configured rate clears the link and keeps the name and rate of the tax.

    Args:
        tenant: Active tenant.
    """
    rate = TaxRate.objects.create(name="Austin", rate_bp=825)
    line = make_line(make_invoice())
    line.save()
    InvoiceLineTax.objects.create(line=line, tax_rate=rate, name="Austin", rate_bp=825, amount=165)
    rate.delete()
    tax = line.taxes.get()
    assert (tax.tax_rate, tax.name, tax.rate_bp, tax.amount) == (None, "Austin", 825, 165)


def test_deleting_a_product_keeps_the_line(tenant: Tenant) -> None:
    """The product link is optional and cleared when the product goes away.

    Args:
        tenant: Active tenant.
    """
    product = Product.objects.create(name="Widget", unit_price=1000)
    line = make_line(make_invoice(), product=product)
    line.save()
    product.delete()
    line.refresh_from_db()
    assert line.product is None


def test_deleting_an_invoice_deletes_its_lines_and_taxes(tenant: Tenant) -> None:
    """Lines and their taxes are removed with the invoice.

    Args:
        tenant: Active tenant.
    """
    invoice = make_invoice()
    line = make_line(invoice)
    line.save()
    InvoiceLineTax.objects.create(line=line, name="Austin", rate_bp=825, amount=165)
    invoice.delete()
    assert (InvoiceLine.objects.count(), InvoiceLineTax.objects.count()) == (0, 0)


def test_the_invoice_admin_is_read_only_and_lists_status_customer_and_total() -> None:
    """The admin lists the headline columns, shows lines read-only and allows no writes."""
    model_admin = admin.site._registry[Invoice]
    assert {"status", "customer", "total"} <= set(model_admin.list_display)
    inline = model_admin.inlines[0](Invoice, admin.site)
    request = RequestFactory().get("/")
    assert inline.model is InvoiceLine
    assert not (
        inline.has_add_permission(request, None)
        or inline.has_change_permission(request)
        or inline.has_delete_permission(request)
    )


def test_the_models_describe_themselves(tenant: Tenant) -> None:
    """Each model has a readable label built from its own fields.

    Args:
        tenant: Active tenant.
    """
    invoice = make_invoice()
    line = make_line(invoice)
    line.save()
    tax = InvoiceLineTax.objects.create(line=line, name="Austin", rate_bp=825, amount=165)
    assert str(invoice) == "Acme draft invoice"
    assert (str(line), str(tax)) == ("Widget", "Austin 825 bp")
