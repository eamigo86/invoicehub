"""Reversing payments: invoices are restored, the payment is deleted and the audit remembers."""

from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied

from billing.models import AuditEvent, Customer, Invoice, InvoiceEmail, Payment, PaymentAllocation
from billing.services import (
    DraftLine,
    InvalidTransition,
    draft_credit_note,
    issue_invoice,
    reverse_payment,
)
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice, pay, stored

pytestmark = pytest.mark.django_db


def state(invoice: Invoice) -> tuple[Any, ...]:
    """Capture what a refused reversal must leave alone.

    Args:
        invoice: Invoice to read again.

    Returns:
        Its status, and the payment, allocation and audit counts.
    """
    return (
        stored(invoice).status,
        Payment.objects.count(),
        PaymentAllocation.objects.count(),
        AuditEvent.objects.count(),
    )


def test_a_full_payment_is_reversed_to_issued(writer: Principal, customer: Customer) -> None:
    """The payment and allocation are gone and the invoice is issued again.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    payment = pay(writer, [(invoice, 1000)])
    reverse_payment(writer, payment)
    assert stored(invoice).status == Invoice.Status.ISSUED
    assert stored(invoice).balance_due() == 1000
    assert not Payment.objects.exists() and not PaymentAllocation.objects.exists()


def test_an_invoice_that_was_sent_goes_back_to_sent(writer: Principal, customer: Customer) -> None:
    """A delivered email (sent_at set) means the invoice returns to sent, not issued.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    InvoiceEmail.objects.create(
        invoice=invoice,
        idempotency_key="k",
        recipients=["a@example.com"],
        sent_at=invoice.created_at,
    )
    payment = pay(writer, [(invoice, 1000)])
    reverse_payment(writer, payment)
    assert stored(invoice).status == Invoice.Status.SENT


def test_an_undelivered_email_does_not_make_the_invoice_sent(
    writer: Principal, customer: Customer
) -> None:
    """A request without sent_at was never delivered, so the invoice returns to issued.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    InvoiceEmail.objects.create(invoice=invoice, idempotency_key="k", recipients=["a@example.com"])
    reverse_payment(writer, pay(writer, [(invoice, 1000)]))
    assert stored(invoice).status == Invoice.Status.ISSUED


def test_one_of_two_partial_payments_leaves_the_invoice_partially_paid(
    writer: Principal, customer: Customer
) -> None:
    """The other payment still counts, so the status and the balance follow it.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    first = pay(writer, [(invoice, 400)])
    pay(writer, [(invoice, 600)])
    assert stored(invoice).status == Invoice.Status.PAID
    reverse_payment(writer, first)
    assert stored(invoice).status == Invoice.Status.PARTIALLY_PAID
    assert stored(invoice).balance_due() == 400


def test_a_payment_across_two_invoices_restores_both(writer: Principal, customer: Customer) -> None:
    """Both invoices return to issued and each gets its audit event.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    first = issued_invoice(writer, customer, 1000)
    second = issued_invoice(writer, customer, 1000)
    payment = pay(writer, [(first, 1000), (second, 300)])
    reverse_payment(writer, payment)
    assert stored(first).status == stored(second).status == Invoice.Status.ISSUED
    assert stored(second).balance_due() == 1000
    assert AuditEvent.objects.filter(action="invoice.payment_reversed").count() == 2


def test_the_reversal_is_audited_with_before_and_after(
    writer: Principal, customer: Customer
) -> None:
    """The event names the user and mirrors payment_recorded in reverse.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    payment = pay(writer, [(invoice, 1000)])
    payment_id = payment.pk
    reverse_payment(writer, payment)
    event = AuditEvent.objects.get(action="invoice.payment_reversed")
    assert (event.entity, event.entity_id) == ("invoice", invoice.pk)
    assert event.actor_user_id == writer.user_id
    assert {k: v for k, v in event.before.items() if k != "payment"} == {
        "status": "paid",
        "balance": 0,
    }
    assert event.after == {"status": "issued", "balance": 1000, "payment_id": payment_id}


def test_invoices_write_is_required(writer: Principal, customer: Customer) -> None:
    """Without invoices:write the reversal is refused and nothing changes.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    payment = pay(writer, [(invoice, 1000)])
    weak = Principal(writer.tenant_id, frozenset({Permission.RECONCILIATION_WRITE}))
    before = state(invoice)
    with pytest.raises(PermissionDenied):
        reverse_payment(weak, payment)
    assert state(invoice) == before


def test_a_voided_invoice_is_not_reopened(writer: Principal, customer: Customer) -> None:
    """A payment of a voided invoice cannot be reversed, and nothing is written.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer)
    other = issued_invoice(writer, customer)
    payment = pay(writer, [(invoice, 1000), (other, 1000)])
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.VOIDED)
    before = state(other)
    with pytest.raises(InvalidTransition):
        reverse_payment(writer, payment)
    assert state(other) == before
    assert stored(invoice).status == Invoice.Status.VOIDED


def credit(writer: Principal, invoice: Invoice, cents: int) -> None:
    """Issue a partial credit note for an invoice.

    Args:
        writer: Principal allowed to write invoices.
        invoice: Invoice to credit.
        cents: Amount credited.
    """
    note = draft_credit_note(writer, invoice, [DraftLine("Credit", Decimal(1), cents)])
    issue_invoice(writer, note)


def test_an_invoice_settled_by_payments_and_credit_stays_paid(
    writer: Principal, customer: Customer
) -> None:
    """Reversing a payment keeps PAID when the remaining payments and credits cover the total.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    invoice = issued_invoice(writer, customer, 1000)
    pay(writer, [(invoice, 400)])
    second = pay(writer, [(invoice, 600)])
    credit(writer, invoice, 600)
    reverse_payment(writer, second)
    assert stored(invoice).balance_due() == 0
    assert stored(invoice).status == Invoice.Status.PAID


@pytest.mark.parametrize("delivered", [False, True])
def test_a_partly_credited_invoice_without_payments_is_open_again(
    writer: Principal, customer: Customer, delivered: bool
) -> None:
    """With no payment left, the invoice is issued or sent and owes the total less the credit.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        delivered: Whether an email of the invoice was delivered.
    """
    invoice = issued_invoice(writer, customer, 1000)
    if delivered:
        InvoiceEmail.objects.create(
            invoice=invoice,
            idempotency_key="k",
            recipients=["a@example.com"],
            sent_at=invoice.created_at,
        )
    credit(writer, invoice, 300)
    payment = pay(writer, [(invoice, 700)])
    assert stored(invoice).status == Invoice.Status.PAID
    reverse_payment(writer, payment)
    assert stored(invoice).balance_due() == 700
    assert stored(invoice).status == (Invoice.Status.SENT if delivered else Invoice.Status.ISSUED)


def test_the_audit_keeps_the_money_of_the_deleted_payment(
    writer: Principal, customer: Customer
) -> None:
    """The before state carries the payment and its allocations, since the row is deleted.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    first = issued_invoice(writer, customer, 1000)
    second = issued_invoice(writer, customer, 1000)
    payment = pay(writer, [(first, 1000), (second, 300)], amount=1500)
    expected = {
        "id": payment.pk,
        "amount": 1500,
        "currency": "USD",
        "value_date": payment.value_date.isoformat(),
        "allocations": sorted(
            [
                {"invoice_id": str(first.pk), "amount": 1000},
                {"invoice_id": str(second.pk), "amount": 300},
            ],
            key=lambda a: str(a["invoice_id"]),
        ),
    }
    reverse_payment(writer, payment)
    for event in AuditEvent.objects.filter(action="invoice.payment_reversed"):
        assert event.before["payment"] == expected
