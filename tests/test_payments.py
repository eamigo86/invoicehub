"""Recording payments: allocations, paid status, refusals, audit and concurrency."""

import threading
from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from accounts.models import User
from billing.models import AuditEvent, Customer, Invoice, Payment, PaymentAllocation
from billing.services import (
    DraftLine,
    InvalidTransition,
    draft_credit_note,
    issue_invoice,
    record_payment,
    save_draft,
)
from core.models import Currency
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db

VALUE_DATE = date(2026, 6, 15)


def make_tenant() -> Tenant:
    """Create a tenant with complete issuer details.

    Returns:
        The tenant.
    """
    return Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant and make it the active one.

    Yields:
        The tenant.
    """
    tenant = make_tenant()
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


def issued_invoice(
    writer: Principal, customer: Customer, cents: int = 1000, currency: str = Currency.USD
) -> Invoice:
    """Draft and issue an invoice of one line.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        cents: Price of the single line, which is also the total.
        currency: Currency of the invoice.

    Returns:
        The issued invoice.
    """
    draft = save_draft(
        writer,
        Invoice(customer=customer, currency=currency),
        [DraftLine("Widget", Decimal(1), cents)],
    )
    return issue_invoice(writer, draft)


def pay(
    writer: Principal,
    allocations: list[tuple[Invoice, int]],
    amount: int | None = None,
    currency: str = Currency.USD,
) -> Payment:
    """Record a manual payment, by default exactly as large as its allocations.

    Args:
        writer: Principal allowed to write invoices.
        allocations: Invoices and the cents applied to each.
        amount: Amount of the payment, or the sum of the allocations when omitted.
        currency: Currency of the payment.

    Returns:
        The recorded payment.
    """
    total = amount if amount is not None else sum(cents for _, cents in allocations)
    payment = Payment(amount=total, currency=currency, value_date=VALUE_DATE)
    return record_payment(writer, payment, allocations)


def stored(invoice: Invoice) -> Invoice:
    """Reload an invoice from the database.

    Args:
        invoice: Invoice to reload.

    Returns:
        The stored copy.
    """
    reloaded: Invoice = Invoice.objects.get(pk=invoice.pk)
    return reloaded


def test_a_full_payment_marks_the_invoice_paid(writer: Principal, customer: Customer) -> None:
    """Paying the whole balance stores the payment and allocation and sets paid.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    payment = pay(writer, [(invoice, 1000)])
    assert stored(invoice).status == Invoice.Status.PAID
    assert (payment.pk, payment.source) == (Payment.objects.get().pk, Payment.Source.MANUAL)
    allocation = PaymentAllocation.objects.get()
    assert (allocation.payment_id, allocation.invoice_id, allocation.amount) == (
        payment.pk,
        invoice.pk,
        1000,
    )


def test_a_partial_payment_marks_the_invoice_partially_paid(
    writer: Principal, customer: Customer
) -> None:
    """Paying less than the balance sets partially paid, and the rest later sets paid.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    pay(writer, [(invoice, 400)])
    assert stored(invoice).status == Invoice.Status.PARTIALLY_PAID
    assert stored(invoice).balance_due() == 600
    pay(writer, [(invoice, 600)])
    assert stored(invoice).status == Invoice.Status.PAID
    assert stored(invoice).balance_due() == 0


def test_one_payment_can_be_split_across_two_invoices(
    writer: Principal, customer: Customer
) -> None:
    """One payment settles one invoice fully and another partly.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    first = issued_invoice(writer, customer, 1000)
    second = issued_invoice(writer, customer, 500)
    pay(writer, [(second, 200), (first, 1000)])
    assert (stored(first).status, stored(second).status) == (
        Invoice.Status.PAID,
        Invoice.Status.PARTIALLY_PAID,
    )
    assert Payment.objects.count() == 1
    assert PaymentAllocation.objects.count() == 2


def test_a_remainder_stays_unallocated(writer: Principal, customer: Customer) -> None:
    """A payment larger than its allocations is stored, and so is one with no allocation.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    pay(writer, [(invoice, 300)], amount=1000)
    pay(writer, [], amount=50)
    assert Payment.objects.count() == 2
    assert PaymentAllocation.objects.get().amount == 300
    assert stored(invoice).balance_due() == 700


def test_balance_due_subtracts_credit_notes_and_payments(
    writer: Principal, customer: Customer
) -> None:
    """The balance is the total minus issued credit notes minus allocations.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    assert invoice.balance_due() == 1000
    note = draft_credit_note(writer, invoice, [DraftLine("Credit", Decimal(1), 100)])
    assert invoice.balance_due() == 1000  # a draft credit note changes nothing
    issue_invoice(writer, note)
    pay(writer, [(invoice, 250)])
    assert invoice.balance_due() == 650


def test_over_allocating_an_invoice_is_refused_and_nothing_is_stored(
    writer: Principal, customer: Customer
) -> None:
    """An allocation above the balance of its invoice raises and stores nothing.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    pay(writer, [(invoice, 400)])
    with pytest.raises(ValidationError):
        pay(writer, [(invoice, 601)])
    assert (Payment.objects.count(), PaymentAllocation.objects.count()) == (1, 1)
    assert stored(invoice).status == Invoice.Status.PARTIALLY_PAID


def test_allocations_above_the_payment_amount_are_refused(
    writer: Principal, customer: Customer
) -> None:
    """The allocations together cannot exceed what the payment brings.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    first = issued_invoice(writer, customer)
    second = issued_invoice(writer, customer)
    with pytest.raises(ValidationError):
        pay(writer, [(first, 600), (second, 600)], amount=1000)
    assert (Payment.objects.count(), PaymentAllocation.objects.count()) == (0, 0)
    assert stored(first).status == stored(second).status == Invoice.Status.ISSUED


@pytest.mark.parametrize("cents", [0, -5])
def test_non_positive_allocations_are_refused(
    writer: Principal, customer: Customer, cents: int
) -> None:
    """An allocation must be positive.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        cents: Allocation amount under test.
    """
    invoice = issued_invoice(writer, customer)
    with pytest.raises(ValidationError):
        pay(writer, [(invoice, cents)], amount=100)
    assert Payment.objects.count() == 0


def test_a_non_positive_payment_is_refused(writer: Principal, customer: Customer) -> None:
    """A payment of zero cents is refused by validation.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    with pytest.raises(ValidationError):
        pay(writer, [(invoice, 0)], amount=0)
    assert Payment.objects.count() == 0


def test_the_same_invoice_twice_is_refused(writer: Principal, customer: Customer) -> None:
    """One payment allocates to an invoice at most once.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    with pytest.raises(ValidationError):
        pay(writer, [(invoice, 100), (invoice, 100)])
    assert Payment.objects.count() == 0


def test_a_wrong_currency_is_refused_and_nothing_is_stored(
    writer: Principal, customer: Customer
) -> None:
    """A payment in another currency than its invoice is refused.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer, currency=Currency.USD)
    with pytest.raises(ValidationError):
        pay(writer, [(invoice, 500)], currency=Currency.CAD)
    assert (Payment.objects.count(), PaymentAllocation.objects.count()) == (0, 0)
    assert stored(invoice).status == Invoice.Status.ISSUED


def test_a_draft_cannot_be_paid(writer: Principal, customer: Customer) -> None:
    """A draft invoice is refused with InvalidTransition and nothing is stored.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    draft = save_draft(writer, Invoice(customer=customer), [DraftLine("W", Decimal(1), 100)])
    with pytest.raises(InvalidTransition):
        pay(writer, [(draft, 100)])
    assert Payment.objects.count() == 0


def test_a_paid_invoice_cannot_be_paid_again(writer: Principal, customer: Customer) -> None:
    """A paid invoice is refused.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    pay(writer, [(invoice, 1000)])
    with pytest.raises(InvalidTransition):
        pay(writer, [(invoice, 1)])
    assert Payment.objects.count() == 1


def test_a_voided_invoice_cannot_be_paid(writer: Principal, customer: Customer) -> None:
    """A voided invoice is refused.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    issue_invoice(writer, draft_credit_note(writer, invoice))
    assert stored(invoice).status == Invoice.Status.VOIDED
    with pytest.raises(InvalidTransition):
        pay(writer, [(invoice, 1)])
    assert Payment.objects.count() == 0


def test_a_credit_note_cannot_be_paid(writer: Principal, customer: Customer) -> None:
    """Only invoices are paid, never credit notes.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    note = issue_invoice(
        writer, draft_credit_note(writer, invoice, [DraftLine("Credit", Decimal(1), 100)])
    )
    with pytest.raises(InvalidTransition):
        pay(writer, [(note, 100)])
    assert Payment.objects.count() == 0


def test_an_unknown_invoice_is_refused(writer: Principal, customer: Customer) -> None:
    """An invoice that is not stored for the tenant raises DoesNotExist.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    ghost = Invoice(customer=customer, currency=Currency.USD)
    with pytest.raises(Invoice.DoesNotExist):
        pay(writer, [(ghost, 100)])
    assert Payment.objects.count() == 0


def test_a_viewer_cannot_record_a_payment(
    tenant: Tenant, writer: Principal, customer: Customer
) -> None:
    """A principal without invoices:write is refused and nothing is stored.

    Args:
        tenant: Active tenant.
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        pay(viewer, [(invoice, 100)])
    assert Payment.objects.count() == 0


def test_each_invoice_gets_a_payment_event(
    writer: Principal, user: User, customer: Customer
) -> None:
    """Every invoice paid records invoice.payment_recorded with status and balance before and after.

    Args:
        writer: Principal allowed to write invoices.
        user: User behind the principal.
        customer: Customer to invoice.
    """
    first = issued_invoice(writer, customer, 1000)
    second = issued_invoice(writer, customer, 500)
    payment = pay(writer, [(first, 1000), (second, 200)])
    events = AuditEvent.objects.filter(action="invoice.payment_recorded")
    by_invoice = {event.entity_id: event for event in events}
    assert set(by_invoice) == {first.pk, second.pk}
    done = by_invoice[first.pk]
    assert done.before == {"status": "issued", "balance": 1000}
    assert done.after == {"status": "paid", "balance": 0, "payment_id": payment.pk}
    assert (done.actor_kind, done.actor_user_id) == (AuditEvent.ActorKind.USER, user.pk)
    part = by_invoice[second.pk]
    assert part.after == {"status": "partially_paid", "balance": 300, "payment_id": payment.pk}


@pytest.mark.django_db(transaction=True)
def test_concurrent_payments_cannot_overpay_an_invoice() -> None:
    """Two threads each paying 60 percent of one balance: one succeeds, the other is refused.

    The tenant fixtures are not used, because they keep an open transaction that the threads
    could not see; the setup commits its own tenant contexts instead.
    """
    tenant = make_tenant()
    writer = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        invoice = issued_invoice(writer, Customer.objects.create(name="Bolt"))
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def work() -> None:
        """Pay 600 of the 1000 balance in its own connection and tenant context."""
        try:
            with tenant_context(tenant.pk):
                barrier.wait(timeout=10)
                try:
                    pay(writer, [(invoice, 600)])
                except ValidationError:
                    outcomes.append("refused")
                else:
                    outcomes.append("paid")
        except BaseException as error:  # noqa: BLE001 - collected and asserted below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(outcomes) == ["paid", "refused"]
    with tenant_context(tenant.pk):
        assert stored(invoice).status == Invoice.Status.PARTIALLY_PAID
        assert stored(invoice).balance_due() == 400
        assert Payment.objects.count() == 1
