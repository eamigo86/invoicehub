"""The billing seed: expected content, determinism, consistency, isolation and the command."""

from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.db.models import Sum
from django.utils import timezone

from billing.models import (
    Customer,
    Invoice,
    InvoiceLine,
    Payment,
    Product,
    TaxRate,
)
from billing.seed import seed_tenant
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def seeded(seed: int = 1, name: str = "Demo") -> Tenant:
    """Create a tenant and seed it.

    Args:
        seed: Seed of the generator.
        name: Name of the new tenant.

    Returns:
        The seeded tenant.
    """
    tenant = Tenant.objects.create(name=name)
    seed_tenant(tenant.pk, seed)
    return tenant


def fingerprint(tenant: Tenant) -> tuple[list[str], list[int]]:
    """Read the customer names and the invoice totals of a tenant.

    Args:
        tenant: Tenant to read.

    Returns:
        The customer names and the invoice totals, both in a stable order.
    """
    with tenant_context(tenant.pk):
        names = list(Customer.objects.order_by("name").values_list("name", flat=True))
        totals = list(Invoice.objects.order_by("id").values_list("total", flat=True))
    return names, sorted(totals)


def test_the_seeded_tenant_has_the_expected_counts() -> None:
    """Issuer, rates, customers, products and invoices are all filled in."""
    tenant = seeded()
    tenant.refresh_from_db()
    assert tenant.legal_name
    assert tenant.default_currency == "USD"
    assert tenant.ein[2] == "-"
    assert len(tenant.ein) == 10
    with tenant_context(tenant.pk):
        assert TaxRate.objects.count() in (2, 3)
        assert Customer.objects.count() == 8
        assert Product.objects.count() == 6
        assert Invoice.objects.filter(kind=Invoice.Kind.INVOICE).count() == 12
        assert Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE).count() == 2
        assert all(c.emails and c.city for c in Customer.objects.all())


def test_the_seed_covers_every_status() -> None:
    """Drafts, unpaid, overdue, partially paid, paid, voided and a partial credit note exist."""
    tenant = seeded()
    with tenant_context(tenant.pk):
        today = timezone.now().astimezone(ZoneInfo(Tenant.objects.get().timezone)).date()
        invoices = list(Invoice.objects.filter(kind=Invoice.Kind.INVOICE))
        by_status = {s: [i for i in invoices if i.status == s] for s in Invoice.Status.values}
        assert by_status["draft"]
        assert by_status["partially_paid"]
        assert by_status["paid"]
        assert len(by_status["voided"]) == 1
        unpaid = by_status["issued"]
        assert any(i.is_overdue(today) for i in unpaid)
        assert any(not i.is_overdue(today) for i in unpaid)
        notes = Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE)
        assert notes.filter(original=by_status["voided"][0]).count() == 1
        assert notes.exclude(original=by_status["voided"][0]).filter(status="issued").count() == 1
        assert all(i.issue_date and i.issue_date <= today for i in invoices if i.issue_date)
        assert Payment.objects.exists()


def test_the_same_seed_gives_the_same_content_in_two_tenants() -> None:
    """Names and invoice totals depend only on the seed."""
    assert fingerprint(seeded(7, "A")) == fingerprint(seeded(7, "B"))
    assert fingerprint(seeded(7, "C")) != fingerprint(seeded(8, "D"))


def test_every_stored_total_equals_the_sum_of_its_lines() -> None:
    """Subtotal, tax total and total match the lines and their taxes."""
    tenant = seeded()
    with tenant_context(tenant.pk):
        assert Invoice.objects.count() == 14
        for invoice in Invoice.objects.all():
            lines = InvoiceLine.objects.filter(invoice=invoice)
            net = lines.aggregate(t=Sum("net"))["t"]
            gross = lines.aggregate(t=Sum("total"))["t"]
            assert invoice.subtotal == net
            assert invoice.total == gross == invoice.subtotal + invoice.tax_total


@pytest.mark.isolation
def test_seeding_one_tenant_writes_nothing_visible_to_another() -> None:
    """Tenant B sees none of the rows seeded for tenant A."""
    seeded(1, "A")
    with tenant_context(Tenant.objects.get(name="A").pk):
        assert Invoice.objects.exists()
    other = Tenant.objects.create(name="B")
    with tenant_context(other.pk):
        for model in (Customer, Product, TaxRate, Invoice, InvoiceLine, Payment):
            assert model.objects.count() == 0


def test_the_command_creates_the_requested_number_of_tenants() -> None:
    """Each run creates new tenants, so running twice never fails."""
    out = StringIO()
    call_command("seed_billing", "--tenants", "2", stdout=out)
    assert Tenant.objects.count() == 2
    call_command("seed_billing", "--tenants", "1", "--seed", "5", stdout=out)
    assert Tenant.objects.count() == 3
    assert "customers" in out.getvalue()
