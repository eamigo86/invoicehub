"""Credit notes: full and partial credits, voiding the original, limits and refusals."""

from collections.abc import Generator
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from accounts.models import User
from billing.models import AuditEvent, Customer, Invoice, InvoiceSeries, TaxRate
from billing.services import (
    DraftLine,
    InvalidTransition,
    draft_credit_note,
    issue_invoice,
    save_draft,
)
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant with complete issuer details and make it the active one.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    with tenant_context(tenant.pk):
        yield tenant


@pytest.fixture
def user() -> User:
    """Create the user behind the writer principal.

    Returns:
        The user.
    """
    return User.objects.create_user("ann", "ann@example.com", "pw")


@pytest.fixture
def writer(tenant: Tenant, user: User) -> Principal:
    """Build a principal that may write invoices, acting as the user.

    Args:
        tenant: Active tenant.
        user: User the principal stands for.

    Returns:
        The principal.
    """
    permissions = frozenset({Permission.INVOICES_READ, Permission.INVOICES_WRITE})
    return Principal(tenant.pk, permissions, user_id=user.pk)


@pytest.fixture
def customer(tenant: Tenant) -> Customer:
    """Create a customer in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The customer.
    """
    created: Customer = Customer.objects.create(name="Bolt")
    return created


@pytest.fixture
def austin(tenant: Tenant) -> TaxRate:
    """Create a tax rate in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The tax rate.
    """
    created: TaxRate = TaxRate.objects.create(name="Austin", rate_bp=825)
    return created


def issued_invoice(
    writer: Principal, customer: Customer, lines: list[DraftLine] | None = None
) -> Invoice:
    """Draft and issue an invoice of 1000 cents, unless other lines are given.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        lines: Lines of the invoice, replacing the default single line.

    Returns:
        The issued invoice.
    """
    lines = lines or [DraftLine("Widget", Decimal(1), 1000)]
    return issue_invoice(writer, save_draft(writer, Invoice(customer=customer), lines))


def credit(writer: Principal, original: Invoice, cents: int) -> Invoice:
    """Issue a partial credit note of one line for an amount.

    Args:
        writer: Principal allowed to write invoices.
        original: Invoice to credit.
        cents: Amount of the credit in cents.

    Returns:
        The issued credit note.
    """
    note = draft_credit_note(writer, original, [DraftLine("Credit", Decimal(1), cents)])
    return issue_invoice(writer, note)


def status_of(invoice: Invoice) -> str:
    """Read the stored status of an invoice.

    Args:
        invoice: Invoice to look up.

    Returns:
        Its status in the database.
    """
    status: str = Invoice.objects.get(pk=invoice.pk).status
    return status


def test_a_full_credit_equals_the_original_even_after_the_tax_rate_changed(
    writer: Principal, customer: Customer, austin: TaxRate
) -> None:
    """The credit note copies lines and tax snapshots, so its total equals the original total.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        austin: Tax rate applied to the original.
    """
    original = issued_invoice(
        writer,
        customer,
        [
            DraftLine("Widget", Decimal("3"), 1999, 500, (austin,)),
            DraftLine("Service", Decimal("1.5"), 4999),
        ],
    )
    austin.name, austin.rate_bp = "Austin 2027", 1000
    austin.save()
    note = draft_credit_note(writer, original)
    assert (note.kind, note.status, note.original_id) == (
        Invoice.Kind.CREDIT_NOTE,
        Invoice.Status.DRAFT,
        original.pk,
    )
    assert (note.customer_id, note.currency) == (original.customer_id, original.currency)
    assert (note.subtotal, note.tax_total, note.total) == (
        original.subtotal,
        original.tax_total,
        original.total,
    )
    assert original.tax_total > 0

    def shape(invoice: Invoice) -> list[tuple[object, ...]]:
        """Describe the lines of an invoice with their taxes.

        Args:
            invoice: Invoice to describe.

        Returns:
            One tuple per line in position order.
        """
        return [
            (
                line.position,
                line.description,
                line.quantity,
                line.unit_price,
                line.discount_bp,
                line.net,
                line.total,
                [(t.name, t.rate_bp, t.amount) for t in line.taxes.order_by("id")],
            )
            for line in invoice.lines.order_by("position")
        ]

    assert shape(note) == shape(original)
    first_tax = note.lines.get(position=1).taxes.get()
    assert (first_tax.name, first_tax.rate_bp) == ("Austin", 825)


def test_issuing_a_full_credit_voids_the_original_and_audits_it(
    writer: Principal, customer: Customer, user: User
) -> None:
    """The credit that reaches the original total voids it and records invoice.voided.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        user: User behind the principal.
    """
    original = issued_invoice(writer, customer)
    note = issue_invoice(writer, draft_credit_note(writer, original))
    assert (note.reference, note.status) == (f"CN-{note.year}-00001", Invoice.Status.ISSUED)
    assert status_of(original) == Invoice.Status.VOIDED
    event = AuditEvent.objects.get(action="invoice.voided")
    assert (event.entity, event.entity_id, event.actor_user_id) == (
        "invoice",
        original.pk,
        user.pk,
    )
    assert (event.before, event.after) == ({"status": "issued"}, {"status": "voided"})


def test_partial_credits_accumulate_and_the_one_reaching_the_total_voids(
    writer: Principal, customer: Customer
) -> None:
    """Credits below the total leave the original open and the last one voids it.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    original = issued_invoice(writer, customer)
    credit(writer, original, 400)
    credit(writer, original, 400)
    assert status_of(original) == Invoice.Status.ISSUED
    assert not AuditEvent.objects.filter(action="invoice.voided").exists()
    credit(writer, original, 200)
    assert status_of(original) == Invoice.Status.VOIDED
    assert AuditEvent.objects.filter(action="invoice.voided").count() == 1


def test_a_credit_beyond_the_remaining_amount_is_refused_and_changes_nothing(
    writer: Principal, customer: Customer
) -> None:
    """Issuing a credit that would exceed the original total raises and rolls back.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    original = issued_invoice(writer, customer)
    credit(writer, original, 700)
    excess = draft_credit_note(writer, original, [DraftLine("Credit", Decimal(1), 400)])
    with pytest.raises(ValidationError, match="exceed"):
        issue_invoice(writer, excess)
    stored = Invoice.objects.get(pk=excess.pk)
    assert (stored.status, stored.number, stored.reference) == (Invoice.Status.DRAFT, None, "")
    assert status_of(original) == Invoice.Status.ISSUED
    assert InvoiceSeries.objects.get(kind=Invoice.Kind.CREDIT_NOTE).next_number == 2
    assert not AuditEvent.objects.filter(action="invoice.voided").exists()


def test_a_second_full_credit_cannot_be_issued_after_the_first_voided_the_original(
    writer: Principal, customer: Customer
) -> None:
    """The original must still be creditable when its credit note is issued.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    original = issued_invoice(writer, customer)
    first = draft_credit_note(writer, original)
    second = draft_credit_note(writer, original)
    issue_invoice(writer, first)
    with pytest.raises(InvalidTransition):
        issue_invoice(writer, second)
    assert status_of(second) == Invoice.Status.DRAFT


def test_a_draft_original_cannot_be_credited(writer: Principal, customer: Customer) -> None:
    """Only issued documents are corrected with credit notes.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    draft = save_draft(writer, Invoice(customer=customer), [DraftLine("A", Decimal(1), 5)])
    with pytest.raises(InvalidTransition):
        draft_credit_note(writer, draft)
    assert Invoice.objects.count() == 1


def test_a_voided_original_cannot_be_credited_again(writer: Principal, customer: Customer) -> None:
    """A voided invoice is closed: no further credit note can be drafted for it.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    original = issued_invoice(writer, customer)
    issue_invoice(writer, draft_credit_note(writer, original))
    with pytest.raises(InvalidTransition):
        draft_credit_note(writer, original)
    assert Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE).count() == 1


@pytest.mark.parametrize(
    "status",
    [Invoice.Status.SENT, Invoice.Status.PARTIALLY_PAID, Invoice.Status.PAID],
)
def test_sent_and_paid_originals_can_be_credited(
    writer: Principal, customer: Customer, status: Invoice.Status
) -> None:
    """Issued, sent, partially paid and paid invoices are all creditable.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        status: Status the original is moved to.
    """
    original = issued_invoice(writer, customer)
    Invoice.objects.filter(pk=original.pk).update(status=status)
    note = draft_credit_note(writer, original)
    assert note.original_id == original.pk


def test_a_credit_note_cannot_be_credited(writer: Principal, customer: Customer) -> None:
    """A credit note is not an invoice, so it cannot be the original of another one.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    original = issued_invoice(writer, customer)
    note = credit(writer, original, 100)
    with pytest.raises(InvalidTransition):
        draft_credit_note(writer, note)


def test_credit_notes_are_numbered_in_their_own_series(
    writer: Principal, customer: Customer
) -> None:
    """Invoices count in INV- and credit notes in CN-, independently.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    first = issued_invoice(writer, customer)
    second = issued_invoice(writer, customer)
    note = credit(writer, first, 100)
    other = credit(writer, second, 100)
    assert [i.reference.split("-")[0] for i in (first, second, note, other)] == [
        "INV",
        "INV",
        "CN",
        "CN",
    ]
    assert (note.number, other.number) == (1, 2)


def test_a_viewer_cannot_draft_a_credit_note(
    tenant: Tenant, writer: Principal, customer: Customer
) -> None:
    """A principal with only invoices:read is refused and nothing is stored.

    Args:
        tenant: Active tenant.
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    original = issued_invoice(writer, customer)
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        draft_credit_note(viewer, original)
    with pytest.raises(PermissionDenied):
        draft_credit_note(None, original)
    assert Invoice.objects.count() == 1


def test_the_kind_decides_whether_an_original_is_required(customer: Customer) -> None:
    """The database refuses a credit note without an original and an invoice with one.

    Args:
        customer: Customer to invoice.
    """
    invoice = Invoice.objects.create(customer=customer, currency="USD")
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(customer=customer, currency="USD", kind=Invoice.Kind.CREDIT_NOTE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(customer=customer, currency="USD", original=invoice)
