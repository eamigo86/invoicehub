"""The invoice document: money format and the HTML that WeasyPrint turns into a PDF."""

from collections.abc import Generator
from decimal import Decimal

import pytest

from billing.models import Customer, Invoice, TaxRate
from billing.pdf import format_money, render_invoice_html
from billing.services import DraftLine, draft_credit_note, issue_invoice, save_draft
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (0, "USD 0.00"),
        (5, "USD 0.05"),
        (100, "USD 1.00"),
        (123456, "USD 1,234.56"),
        (100000000, "USD 1,000,000.00"),
        (-123456, "-USD 1,234.56"),
    ],
)
def test_format_money_uses_integer_cents(cents: int, expected: str) -> None:
    """Amounts group thousands and always show two decimals.

    Args:
        cents: Amount in cents.
        expected: Expected text.
    """
    assert format_money(cents, "USD") == expected


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant with complete issuer details and make it the active one.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(
        name="A",
        legal_name="Acme <Widgets> Inc.",
        ein="12-3456789",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    with tenant_context(tenant.pk):
        yield tenant


def issue(tenant: Tenant) -> Invoice:
    """Issue an invoice with two lines taxed by a state and a county rate.

    Args:
        tenant: Active tenant.

    Returns:
        The issued invoice.
    """
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    customer = Customer.objects.create(name="Bolt & Co", address_line1="9 Side Rd", city="Dallas")
    state = TaxRate.objects.create(name="State", rate_bp=625)
    county = TaxRate.objects.create(name="County", rate_bp=200)
    lines = [
        DraftLine("Widget", Decimal("3"), 333, tax_rates=(state, county)),
        DraftLine("Gadget", Decimal("1.5"), 1001, tax_rates=(state,)),
    ]
    return issue_invoice(principal, save_draft(principal, Invoice(customer=customer), lines))


def test_html_shows_reference_snapshots_lines_breakdown_and_totals(tenant: Tenant) -> None:
    """The document carries every legal element, escaped, with an exact tax breakdown.

    Args:
        tenant: Active tenant.
    """
    invoice = issue(tenant)
    html = render_invoice_html(invoice)
    assert invoice.reference in html
    assert "Acme &lt;Widgets&gt; Inc." in html
    assert "Bolt &amp; Co" in html
    assert "Widget" in html
    assert "Gadget" in html
    assert "1.5" in html
    assert str(invoice.issue_date) in html
    assert str(invoice.due_date) in html
    state_total = sum(
        t.amount for line in invoice.lines.all() for t in line.taxes.all() if t.name == "State"
    )
    county_total = sum(
        t.amount for line in invoice.lines.all() for t in line.taxes.all() if t.name == "County"
    )
    assert state_total + county_total == invoice.tax_total
    assert "State (6.25%)" in html
    assert "County (2.00%)" in html
    assert format_money(state_total, "USD") in html
    assert format_money(county_total, "USD") in html
    for amount in (invoice.subtotal, invoice.tax_total, invoice.total):
        assert format_money(amount, "USD") in html
    assert "Credit note" not in html


def test_credit_note_html_has_its_title_and_the_original_reference(tenant: Tenant) -> None:
    """A credit note is titled Credit note and names the invoice it corrects.

    Args:
        tenant: Active tenant.
    """
    original = issue(tenant)
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    note = issue_invoice(principal, draft_credit_note(principal, original))
    html = render_invoice_html(note)
    assert "Credit note" in html
    assert note.reference in html
    assert original.reference in html
