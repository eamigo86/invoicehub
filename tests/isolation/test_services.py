"""Billing services never act across tenants, whatever the principal holds."""

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from billing.models import (
    AuditEvent,
    Customer,
    Invoice,
    InvoiceEmail,
    InvoiceLine,
    InvoiceSeries,
    Payment,
    PaymentAllocation,
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
    draft_credit_note,
    issue_invoice,
    record_event,
    record_payment,
    reverse_payment,
    save_customer,
    save_draft,
    save_product,
    save_series,
    save_tax_rate,
    send_invoice,
    update_issuer,
)
from core.models import Currency
from reconciliation.models import (
    BankAccount,
    BankStatementImport,
    MatchProposal,
    MatchProposalInvoice,
    ReconciliationRun,
)
from reconciliation.services import (
    accept_proposal,
    create_import,
    reassign_proposal,
    reject_proposal,
    run_reconciliation,
    save_bank_account,
    undo_proposal,
)
from tenancy.context import TenantMismatch, tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice
from tests.test_reconciliation_apply import propose
from tests.test_reconciliation_engine import make_tx
from tests.test_reconciliation_import import make_account

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


def test_b_cannot_credit_an_invoice_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's issued invoice is not found and no credit note is drafted.

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
        issued = issue_invoice(owner_of(a), draft)
    with tenant_context(b.pk), pytest.raises(Invoice.DoesNotExist):
        draft_credit_note(owner_of(b), issued)
    with tenant_context(b.pk):
        assert Invoice.objects.count() == 0
    with tenant_context(a.pk):
        stored = Invoice.objects.get()
        assert (stored.status, stored.kind) == (Invoice.Status.ISSUED, Invoice.Kind.INVOICE)


def test_a_principal_of_a_cannot_credit_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A is refused under B and B's invoice gets no credit note.

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
        issued = issue_invoice(owner_of(b), draft)
        with pytest.raises(PermissionDenied):
            draft_credit_note(owner_of(a), issued)
        assert Invoice.objects.count() == 1


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


def issued_for(tenant: Tenant) -> Invoice:
    """Issue an invoice of 100 cents in a tenant, which must be the active one.

    Args:
        tenant: Active tenant with complete issuer details.

    Returns:
        The issued invoice.
    """
    customer = save_customer(owner_of(tenant), Customer(name="Client"))
    draft = save_draft(
        owner_of(tenant), Invoice(customer=customer), [DraftLine("L", Decimal(1), 100)]
    )
    return issue_invoice(owner_of(tenant), draft)


def test_b_cannot_pay_an_invoice_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's invoice is not found: no payment is stored and A's invoice stays issued.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    with tenant_context(a.pk):
        invoice = issued_for(a)
    payment = Payment(amount=100, currency=Currency.USD, value_date=date(2026, 6, 15))
    with tenant_context(b.pk), pytest.raises(Invoice.DoesNotExist):
        record_payment(owner_of(b), payment, [(invoice, 100)])
    with tenant_context(b.pk):
        assert (Payment.objects.count(), PaymentAllocation.objects.count()) == (0, 0)
    with tenant_context(a.pk):
        assert Invoice.objects.get(pk=invoice.pk).status == Invoice.Status.ISSUED
        assert not PaymentAllocation.objects.exists()


def test_a_principal_of_a_cannot_record_a_payment_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A is refused under B and B's invoice receives nothing.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    payment = Payment(amount=100, currency=Currency.USD, value_date=date(2026, 6, 15))
    with tenant_context(b.pk):
        invoice = issued_for(b)
        with pytest.raises(PermissionDenied):
            record_payment(owner_of(a), payment, [(invoice, 100)])
        assert not Payment.objects.exists()
        assert Invoice.objects.get().status == Invoice.Status.ISSUED


def test_b_cannot_reverse_a_payment_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's payment is not found, and A's principal is refused; A's data is unchanged.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    with tenant_context(a.pk):
        invoice = issued_for(a)
        payment = record_payment(
            owner_of(a),
            Payment(amount=100, currency=Currency.USD, value_date=date(2026, 6, 15)),
            [(invoice, 100)],
        )
    with tenant_context(b.pk):
        with pytest.raises(Payment.DoesNotExist):
            reverse_payment(owner_of(b), payment)
        with pytest.raises(PermissionDenied):
            reverse_payment(owner_of(a), payment)
    with tenant_context(a.pk):
        assert Invoice.objects.get(pk=invoice.pk).status == Invoice.Status.PAID
        assert Payment.objects.count() == 1 and PaymentAllocation.objects.count() == 1
        assert not AuditEvent.objects.filter(action="invoice.payment_reversed").exists()


def test_b_cannot_email_an_invoice_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's issued invoice is not found and no request is stored.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    with tenant_context(a.pk):
        customer = save_customer(owner_of(a), Customer(name="Acme", emails=["ap@a.example"]))
        draft = save_draft(
            owner_of(a), Invoice(customer=customer), [DraftLine("Old", Decimal(1), 5)]
        )
        issued = issue_invoice(owner_of(a), draft)
    with tenant_context(b.pk), pytest.raises(Invoice.DoesNotExist):
        send_invoice(owner_of(b), issued, "k")
    with tenant_context(b.pk):
        assert not InvoiceEmail.objects.exists()
    with tenant_context(a.pk):
        assert not InvoiceEmail.objects.exists()
        assert not AuditEvent.objects.filter(action="invoice.send_requested").exists()


def test_a_principal_of_a_cannot_email_while_b_is_current(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """A principal of A is refused under B and B's invoice gets no request.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    complete_issuer(a, b)
    with tenant_context(b.pk):
        customer = save_customer(owner_of(b), Customer(name="Bolt", emails=["ap@b.example"]))
        draft = save_draft(
            owner_of(b), Invoice(customer=customer), [DraftLine("Old", Decimal(1), 5)]
        )
        issued = issue_invoice(owner_of(b), draft)
        with pytest.raises(PermissionDenied):
            send_invoice(owner_of(a), issued, "k")
        assert not InvoiceEmail.objects.exists()


def test_b_cannot_import_a_statement_into_an_account_of_a(
    tenants: tuple[Tenant, Tenant], media_root: Path
) -> None:
    """Under B, A's bank account is not found: no import, no file, nothing for A either.

    Args:
        tenants: Tenants A and B.
        media_root: Temporary media directory.
    """
    a, b = tenants
    with tenant_context(a.pk):
        account = BankAccount.objects.create(
            name="Chase",
            account_identifier="1234",
            date_column="Date",
            amount_column="Amount",
            description_column="Memo",
        )
    with tenant_context(b.pk), pytest.raises(BankAccount.DoesNotExist):
        create_import(owner_of(b), account, b"Date,Amount,Memo\n")
    with tenant_context(b.pk):
        assert not BankStatementImport.objects.exists()
    with tenant_context(a.pk):
        assert not BankStatementImport.objects.exists()
    assert not media_root.exists()


def test_b_cannot_modify_a_bank_account_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, saving A's bank account is refused and A's row keeps its name.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        account = save_bank_account(
            owner_of(a),
            BankAccount(
                name="Chase",
                account_identifier="1234",
                date_column="Date",
                amount_column="Amount",
                description_column="Memo",
            ),
        )
    account.name = "Hacked"
    with tenant_context(b.pk), pytest.raises(TenantMismatch):
        save_bank_account(owner_of(b), account)
    with tenant_context(a.pk):
        assert BankAccount.objects.get(pk=account.pk).name == "Chase"
    with tenant_context(b.pk):
        assert not BankAccount.objects.exists()


def test_a_pending_run_of_a_is_not_returned_to_b(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, a request creates B's own run instead of returning A's pending run.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        run_of_a = run_reconciliation(owner_of(a))
    with tenant_context(b.pk):
        run_of_b = run_reconciliation(owner_of(b))
        assert run_of_b.pk != run_of_a.pk
        assert [r.pk for r in ReconciliationRun.objects.all()] == [run_of_b.pk]
    with tenant_context(a.pk):
        assert [r.pk for r in ReconciliationRun.objects.all()] == [run_of_a.pk]


def test_a_principal_of_a_cannot_request_a_run_under_b(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's principal is refused and no run is stored in either tenant.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    with tenant_context(b.pk), pytest.raises(PermissionDenied):
        run_reconciliation(owner_of(a))
    for tenant in tenants:
        with tenant_context(tenant.pk):
            assert not ReconciliationRun.objects.exists()


def proposal_of(tenant: Tenant) -> tuple[MatchProposal, Invoice]:
    """Create a pending proposal, with its invoice, in a tenant.

    Args:
        tenant: Tenant that owns the proposal.

    Returns:
        The proposal and the invoice the engine paired.
    """
    Tenant.objects.filter(pk=tenant.pk).update(
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="1",
    )
    with tenant_context(tenant.pk):
        customer = save_customer(owner_of(tenant), Customer(name="Acme"))
        invoice = issued_invoice(owner_of(tenant), customer, 1000)
        proposal = propose("1:1", [make_tx(make_account(), 1000)], [invoice], status="suggested")
    return proposal, invoice


@pytest.mark.parametrize("review", [accept_proposal, reject_proposal])
def test_b_cannot_review_a_proposal_of_a(
    tenants: tuple[Tenant, Tenant], review: Callable[[Principal, MatchProposal], object]
) -> None:
    """Under B, A's proposal is not found for accept and reject, and A's data is unchanged.

    Args:
        tenants: Tenants A and B.
        review: Service under test.
    """
    a, b = tenants
    proposal, _ = proposal_of(a)
    with tenant_context(b.pk), pytest.raises(MatchProposal.DoesNotExist):
        review(owner_of(b), proposal)
    with tenant_context(a.pk):
        stored = MatchProposal.objects.get(pk=proposal.pk)
        assert stored.state == MatchProposal.State.PENDING and stored.decided_at is None
        assert not Payment.objects.exists()
        assert not AuditEvent.objects.filter(action__startswith="reconciliation.").exists()


def test_b_cannot_reassign_a_proposal_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's proposal is not found even when the invoice chosen is B's own.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    proposal, _ = proposal_of(a)
    _, invoice_of_b = proposal_of(b)
    with tenant_context(b.pk), pytest.raises(MatchProposal.DoesNotExist):
        reassign_proposal(owner_of(b), proposal, [invoice_of_b])
    with tenant_context(a.pk):
        assert MatchProposal.objects.get(pk=proposal.pk).state == MatchProposal.State.PENDING
        assert not Payment.objects.exists()
    with tenant_context(b.pk):
        assert not Payment.objects.exists()


def test_a_proposal_of_a_cannot_be_reassigned_to_an_invoice_of_b(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """Under A, an invoice of B is not visible, so the choice is refused and nothing is written.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    proposal, invoice_of_a = proposal_of(a)
    _, invoice_of_b = proposal_of(b)
    with tenant_context(a.pk):
        with pytest.raises(Invoice.DoesNotExist):
            reassign_proposal(owner_of(a), proposal, [invoice_of_b])
        stored = MatchProposal.objects.get(pk=proposal.pk)
        assert stored.state == MatchProposal.State.PENDING and stored.kind == "1:1"
        links = MatchProposalInvoice.objects.filter(proposal=stored)
        assert [link.invoice_id for link in links] == [invoice_of_a.pk]
        assert not Payment.objects.exists()
    with tenant_context(b.pk):
        assert Invoice.objects.get(pk=invoice_of_b.pk).status == Invoice.Status.ISSUED
        assert not Payment.objects.exists()


def test_b_cannot_undo_a_proposal_of_a(tenants: tuple[Tenant, Tenant]) -> None:
    """Under B, A's applied proposal is not found, A's principal is refused, nothing changes.

    Args:
        tenants: Tenants A and B.
    """
    a, b = tenants
    proposal, invoice = proposal_of(a)
    with tenant_context(a.pk):
        accept_proposal(owner_of(a), proposal)
    with tenant_context(b.pk):
        with pytest.raises(MatchProposal.DoesNotExist):
            undo_proposal(owner_of(b), proposal)
        with pytest.raises(PermissionDenied):
            undo_proposal(owner_of(a), proposal)
    with tenant_context(a.pk):
        assert MatchProposal.objects.get(pk=proposal.pk).state == MatchProposal.State.APPLIED
        assert Invoice.objects.get(pk=invoice.pk).status == Invoice.Status.PAID
        assert Payment.objects.count() == 1
        assert not AuditEvent.objects.filter(action__endswith="_undone").exists()
        assert not AuditEvent.objects.filter(action="invoice.payment_reversed").exists()
