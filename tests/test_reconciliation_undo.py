"""Undoing applied proposals: payments are reversed and the bank rows become free again."""

import pytest
from django.core.exceptions import PermissionDenied

from billing.models import AuditEvent, Customer, Invoice, Payment, PaymentAllocation
from billing.services import InvalidTransition, reverse_payment
from reconciliation.models import BankAccount, BankTransaction, MatchProposal
from reconciliation.services import (
    accept_proposal,
    reassign_proposal,
    reject_proposal,
    undo_proposal,
)
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice, pay
from tests.test_reconciliation_apply import propose, status_of
from tests.test_reconciliation_engine import make_tx, start
from tests.test_reconciliation_review import snapshot

pytestmark = pytest.mark.django_db


def test_an_auto_applied_proposal_is_undone_and_matched_again(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """Payment, link and status are reverted, the audit is written and a new run pairs them again.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
    """
    invoice = issued_invoice(writer, customer, 1000)
    tx = make_tx(account, 1000, f"Payment {invoice.reference}")
    start(writer)
    proposal = MatchProposal.objects.get()
    payment_id = Payment.objects.get().pk
    assert proposal.state == MatchProposal.State.APPLIED
    applied_at = proposal.decided_at
    assert applied_at is not None
    undone = undo_proposal(writer, proposal)
    assert undone.pk == proposal.pk and undone.state == MatchProposal.State.UNDONE
    stored = MatchProposal.objects.get(pk=proposal.pk)
    assert stored.state == MatchProposal.State.UNDONE
    assert stored.decided_by_id == writer.user_id and stored.decided_at is not None
    assert not Payment.objects.exists()
    assert BankTransaction.objects.get(pk=tx.pk).payment_id is None
    assert status_of(invoice) == Invoice.Status.ISSUED
    event = AuditEvent.objects.get(action="reconciliation.proposal_undone")
    assert event.entity_id == proposal.pk and event.actor_user_id == writer.user_id
    assert event.before["payments"][0]["id"] == payment_id
    assert event.before["payments"][0]["allocations"] == [
        {"invoice_id": str(invoice.pk), "amount": 1000}
    ]
    assert {k: v for k, v in event.before.items() if k != "payments"} == {
        "state": "applied",
        "payment_ids": [payment_id],
        "invoice_ids": [str(invoice.pk)],
        "decided_by_id": None,
        "decided_at": applied_at.isoformat(),
    }
    assert event.after == {"state": "undone"}
    rerun = start(writer)
    assert rerun.applied_count == 1 and Payment.objects.count() == 1
    assert status_of(invoice) == Invoice.Status.PAID
    assert MatchProposal.objects.count() == 2


def test_a_reassigned_proposal_is_undone(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """The invoice the user chose is restored; the engine's one was never touched.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
    """
    engine_pick = issued_invoice(writer, customer, 1000)
    chosen = issued_invoice(writer, customer, 1000)
    tx = make_tx(account, 1000)
    proposal = propose("1:1", [tx], [engine_pick], status="suggested")
    reassign_proposal(writer, proposal, [chosen])
    undo_proposal(writer, proposal)
    assert MatchProposal.objects.get(pk=proposal.pk).state == MatchProposal.State.UNDONE
    assert status_of(chosen) == status_of(engine_pick) == Invoice.Status.ISSUED
    assert not Payment.objects.exists()
    assert BankTransaction.objects.get(pk=tx.pk).payment_id is None


def test_many_to_one_reverses_both_payments(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """Two transactions paid one invoice with two payments; both go, the invoice reopens.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transactions.
    """
    invoice = issued_invoice(writer, customer, 1000)
    txs = [make_tx(account, 600, tag="a"), make_tx(account, 400, tag="b")]
    proposal = propose("N:1", txs, [invoice])
    accept_proposal(writer, proposal)
    assert Payment.objects.count() == 2 and status_of(invoice) == Invoice.Status.PAID
    undo_proposal(writer, proposal)
    assert not Payment.objects.exists()
    assert status_of(invoice) == Invoice.Status.ISSUED
    assert all(t.payment_id is None for t in BankTransaction.objects.all())
    assert AuditEvent.objects.filter(action="invoice.payment_reversed").count() == 2
    event = AuditEvent.objects.get(action="reconciliation.proposal_undone")
    assert len(event.before["payment_ids"]) == 2 and event.before["invoice_ids"] == [
        str(invoice.pk)
    ]


@pytest.mark.parametrize("reject", [False, True])
def test_a_pending_or_rejected_proposal_cannot_be_undone(
    writer: Principal, customer: Customer, account: BankAccount, reject: bool
) -> None:
    """Only applied or reassigned proposals have payments to undo.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
        reject: Whether the proposal is rejected first.
    """
    invoice = issued_invoice(writer, customer, 1000)
    proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
    if reject:
        reject_proposal(writer, proposal)
    before = snapshot(proposal)
    with pytest.raises(InvalidTransition):
        undo_proposal(writer, proposal)
    assert snapshot(proposal) == before


@pytest.mark.parametrize(
    "permissions", [{Permission.INVOICES_WRITE}, {Permission.RECONCILIATION_WRITE}]
)
def test_a_missing_permission_refuses_and_writes_nothing(
    writer: Principal,
    customer: Customer,
    account: BankAccount,
    permissions: set[Permission],
) -> None:
    """Both reconciliation:write and invoices:write are needed.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
        permissions: The only permissions the acting principal holds.
    """
    invoice = issued_invoice(writer, customer, 1000)
    proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
    accept_proposal(writer, proposal)
    weak = Principal(writer.tenant_id, frozenset(permissions), user_id=writer.user_id)
    before = snapshot(proposal)
    with pytest.raises(PermissionDenied):
        undo_proposal(weak, proposal)
    assert snapshot(proposal) == before


def test_undoing_twice_is_refused(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """The second undo raises InvalidTransition and changes nothing.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
    """
    invoice = issued_invoice(writer, customer, 1000)
    proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
    accept_proposal(writer, proposal)
    undo_proposal(writer, proposal)
    before = snapshot(proposal)
    with pytest.raises(InvalidTransition):
        undo_proposal(writer, proposal)
    assert snapshot(proposal) == before


def test_a_refused_reversal_leaves_the_proposal_applied(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """A voided invoice blocks the undo and nothing is written, the proposal stays applied.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
    """
    first = issued_invoice(writer, customer, 1000)
    second = issued_invoice(writer, customer, 500)
    proposal = propose("1:N", [make_tx(account, 1500)], [first, second])
    accept_proposal(writer, proposal)
    Invoice.objects.filter(pk=second.pk).update(status=Invoice.Status.VOIDED)
    before = snapshot(proposal)
    with pytest.raises(InvalidTransition):
        undo_proposal(writer, proposal)
    assert snapshot(proposal) == before
    assert MatchProposal.objects.get(pk=proposal.pk).state == MatchProposal.State.APPLIED


def test_an_unallocated_payment_is_kept_in_the_proposal_audit(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """The second transaction has nothing to pay, and only this event remembers its money.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transactions.
    """
    invoice = issued_invoice(writer, customer, 1000)
    txs = [make_tx(account, 1000, tag="a"), make_tx(account, 1000, tag="b")]
    proposal = propose("N:1", txs, [invoice])
    accept_proposal(writer, proposal)
    assert PaymentAllocation.objects.count() == 1 and Payment.objects.count() == 2
    undo_proposal(writer, proposal)
    event = AuditEvent.objects.get(action="reconciliation.proposal_undone")
    details = sorted(event.before["payments"], key=lambda p: len(p["allocations"]))
    assert [(p["amount"], p["currency"], len(p["allocations"])) for p in details] == [
        (1000, "USD", 0),
        (1000, "USD", 1),
    ]
    assert details[0]["value_date"] == txs[0].value_date.isoformat()
    assert AuditEvent.objects.filter(action="invoice.payment_reversed").count() == 1


def test_a_payment_already_reversed_is_skipped(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """One payment was reversed by hand; undo handles the other and still ends undone.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transactions.
    """
    invoice = issued_invoice(writer, customer, 1000)
    txs = [make_tx(account, 600, tag="a"), make_tx(account, 400, tag="b")]
    proposal = propose("N:1", txs, [invoice])
    (first, _) = accept_proposal(writer, proposal)
    reverse_payment(writer, first)
    undo_proposal(writer, proposal)
    assert MatchProposal.objects.get(pk=proposal.pk).state == MatchProposal.State.UNDONE
    assert not Payment.objects.exists() and status_of(invoice) == Invoice.Status.ISSUED
    event = AuditEvent.objects.get(action="reconciliation.proposal_undone")
    assert len(event.before["payment_ids"]) == 1


def test_a_manual_payment_on_the_same_invoice_survives(
    writer: Principal, customer: Customer, account: BankAccount
) -> None:
    """Undoing the bank payment leaves the manual one: partially paid with the right balance.

    Args:
        writer: Principal that may write reconciliation data.
        customer: Customer to invoice.
        account: Bank account of the transaction.
    """
    invoice = issued_invoice(writer, customer, 1000)
    manual = pay(writer, [(invoice, 300)])
    proposal = propose("1:1", [make_tx(account, 700)], [invoice])
    accept_proposal(writer, proposal)
    assert status_of(invoice) == Invoice.Status.PAID
    undo_proposal(writer, proposal)
    stored = Invoice.objects.get(pk=invoice.pk)
    assert stored.status == Invoice.Status.PARTIALLY_PAID and stored.balance_due() == 700
    assert list(Payment.objects.values_list("pk", flat=True)) == [manual.pk]
