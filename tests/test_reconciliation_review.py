"""Reviewing proposals: accept, reject and reassign by a user."""

from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from billing.models import AuditEvent, Customer, Invoice, Payment
from billing.services import InvalidTransition
from core.models import Currency
from reconciliation.models import (
    BankAccount,
    BankTransaction,
    MatchProposal,
    MatchProposalInvoice,
    ReconciliationRun,
)
from reconciliation.services import accept_proposal, reassign_proposal, reject_proposal
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice
from tests.test_reconciliation_apply import due_invoice, propose, status_of
from tests.test_reconciliation_engine import make_tx, start

pytestmark = pytest.mark.django_db


def snapshot(proposal: MatchProposal) -> tuple[Any, ...]:
    """Capture everything a refused review must leave alone.

    Args:
        proposal: Proposal to read again.

    Returns:
        The proposal state, kind, decision, invoice links, payment and audit counts, and the
        payments of the bank transactions.
    """
    stored = MatchProposal.objects.get(pk=proposal.pk)
    links = sorted(MatchProposalInvoice.objects.filter(proposal=stored).values_list("invoice_id"))
    return (
        stored.state,
        stored.kind,
        stored.decided_by_id,
        stored.decided_at,
        links,
        Payment.objects.count(),
        AuditEvent.objects.count(),
        sorted(str(t.payment_id) for t in BankTransaction.objects.all()),
    )


class TestAcceptProposal:
    """A user accepts a pending proposal and the money is recorded."""

    @pytest.mark.parametrize("status", ["suggested", "ambiguous"])
    def test_a_doubtful_proposal_is_paid_by_the_user(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        status: str,
    ) -> None:
        """Whatever the engine status, accepting pays and attributes the decision to the user.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
            status: Engine status of the proposal.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000)
        proposal = propose("1:1", [tx], [invoice], status=status)
        (payment,) = accept_proposal(writer, proposal)
        stored = MatchProposal.objects.get(pk=proposal.pk)
        assert stored.state == MatchProposal.State.APPLIED
        assert stored.decided_by_id == writer.user_id and stored.decided_at is not None
        assert status_of(invoice) == Invoice.Status.PAID
        assert BankTransaction.objects.get(pk=tx.pk).payment_id == payment.pk
        event = AuditEvent.objects.get(action="reconciliation.proposal_applied")
        assert event.actor_kind == AuditEvent.ActorKind.USER
        assert event.actor_user_id == writer.user_id

    @pytest.mark.parametrize(
        "permissions",
        [{Permission.INVOICES_WRITE}, {Permission.RECONCILIATION_WRITE}],
    )
    def test_a_missing_permission_refuses_and_writes_nothing(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        permissions: set[Permission],
    ) -> None:
        """Both reconciliation:write and invoices:write are needed to accept.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
            permissions: The only permissions the acting principal holds.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice], status="suggested")
        weak = Principal(writer.tenant_id, frozenset(permissions), user_id=writer.user_id)
        before = snapshot(proposal)
        with pytest.raises(PermissionDenied):
            accept_proposal(weak, proposal)
        assert snapshot(proposal) == before

    def test_a_decided_proposal_cannot_be_accepted_again(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A second accept raises InvalidTransition and records no second payment.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        accept_proposal(writer, proposal)
        before = snapshot(proposal)
        with pytest.raises(InvalidTransition):
            accept_proposal(writer, proposal)
        assert snapshot(proposal) == before


class TestRejectProposal:
    """A user rejects a pending proposal and frees what it held."""

    def test_the_decision_is_recorded_and_the_transaction_is_considered_again(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """State, decision and audit are stored; the next run proposes the transaction again.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        first = issued_invoice(writer, customer, 1000)
        second = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000, f"Pay {first.reference} {second.reference}")
        start(writer)
        proposal = MatchProposal.objects.get()
        returned = reject_proposal(writer, proposal)
        assert returned.pk == proposal.pk and returned.state == MatchProposal.State.REJECTED
        stored = MatchProposal.objects.get(pk=proposal.pk)
        assert stored.state == MatchProposal.State.REJECTED
        assert stored.decided_by_id == writer.user_id and stored.decided_at is not None
        event = AuditEvent.objects.get(action="reconciliation.proposal_rejected")
        assert event.entity_id == proposal.pk and event.actor_user_id == writer.user_id
        assert event.before == {
            "state": "pending",
            "transaction_ids": [str(tx.pk)],
            "invoice_ids": [str(proposal.invoices.get().pk)],
        }
        assert event.after == {"state": "rejected"}
        assert not Payment.objects.exists()
        rerun = start(writer)
        assert (rerun.transactions_considered, rerun.invoices_considered) == (1, 2)
        assert MatchProposal.objects.filter(state="pending").count() == 1

    def test_an_applied_proposal_cannot_be_rejected(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Only pending proposals can be rejected; undoing an applied one is a later feature.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        accept_proposal(writer, proposal)
        before = snapshot(proposal)
        with pytest.raises(InvalidTransition):
            reject_proposal(writer, proposal)
        assert snapshot(proposal) == before

    def test_reconciliation_write_is_required(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A principal without reconciliation:write is refused and nothing changes.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        weak = Principal(writer.tenant_id, frozenset({Permission.INVOICES_WRITE}))
        before = snapshot(proposal)
        with pytest.raises(PermissionDenied):
            reject_proposal(weak, proposal)
        assert snapshot(proposal) == before


class TestReassignProposal:
    """A user keeps the bank transactions and picks other invoices."""

    def test_another_invoice_is_paid_and_the_audit_keeps_both_pairings(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """The chosen invoice is paid, the engine's one is untouched, score and breakdown stay.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        engine_pick = issued_invoice(writer, customer, 1000)
        chosen = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000)
        proposal = propose("1:1", [tx], [engine_pick], status="suggested")
        (payment,) = reassign_proposal(writer, proposal, [chosen])
        stored = MatchProposal.objects.get(pk=proposal.pk)
        assert stored.state == MatchProposal.State.REASSIGNED and stored.kind == "1:1"
        assert stored.decided_by_id == writer.user_id and stored.decided_at is not None
        assert (stored.score, stored.breakdown) == (proposal.score, proposal.breakdown)
        assert status_of(chosen) == Invoice.Status.PAID
        assert status_of(engine_pick) == Invoice.Status.ISSUED
        assert [i.pk for i in stored.invoices.all()] == [chosen.pk]
        assert BankTransaction.objects.get(pk=tx.pk).payment_id == payment.pk
        event = AuditEvent.objects.get(action="reconciliation.proposal_reassigned")
        assert event.actor_user_id == writer.user_id and event.entity_id == proposal.pk
        assert event.before == {
            "state": "pending",
            "kind": "1:1",
            "invoice_ids": [str(engine_pick.pk)],
        }
        assert event.after == {
            "state": "reassigned",
            "kind": "1:1",
            "invoice_ids": [str(chosen.pk)],
            "payment_ids": [payment.pk],
        }
        assert not AuditEvent.objects.filter(action="reconciliation.proposal_applied").exists()

    def test_one_transaction_to_two_invoices_becomes_one_to_many(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """The kind follows the cardinality and the amount fills the earliest due date first.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        engine_pick = issued_invoice(writer, customer, 1200)
        late = due_invoice(writer, customer, 1000, 60)
        early = due_invoice(writer, customer, 500, 10)
        proposal = propose("1:1", [make_tx(account, 1200)], [engine_pick])
        (payment,) = reassign_proposal(writer, proposal, [late, early])
        assert MatchProposal.objects.get(pk=proposal.pk).kind == "1:N"
        event = AuditEvent.objects.get(action="reconciliation.proposal_reassigned")
        assert (event.before["kind"], event.after["kind"]) == ("1:1", "1:N")
        allocated = {a.invoice_id: a.amount for a in payment.allocations.all()}
        assert allocated == {early.pk: 500, late.pk: 700}
        assert status_of(engine_pick) == Invoice.Status.ISSUED

    def test_two_transactions_to_one_invoice_becomes_many_to_one(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Several transactions with a single chosen invoice give N:1 and a payment each.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        engine_pick = issued_invoice(writer, customer, 1000)
        chosen = issued_invoice(writer, customer, 1000)
        txs = [make_tx(account, 600, tag="a"), make_tx(account, 400, tag="b")]
        proposal = propose("N:1", txs, [engine_pick])
        payments = reassign_proposal(writer, proposal, [chosen])
        assert len(payments) == 2 and status_of(chosen) == Invoice.Status.PAID
        assert MatchProposal.objects.get(pk=proposal.pk).kind == "N:1"

    def test_the_transaction_is_not_considered_by_a_later_run(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A reassigned proposal still holds its transaction.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        engine_pick = issued_invoice(writer, customer, 1000)
        chosen = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [engine_pick])
        reassign_proposal(writer, proposal, [chosen])
        rerun = start(writer)
        assert rerun.transactions_considered == 0
        assert MatchProposal.objects.exclude(run=rerun).count() == 1
        assert ReconciliationRun.objects.count() == 2

    def test_invalid_choices_are_refused_and_nothing_is_written(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Empty, duplicated, non-payable, wrong currency and N:M choices change nothing.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        engine_pick = issued_invoice(writer, customer, 1000)
        good = issued_invoice(writer, customer, 1000)
        voided = issued_invoice(writer, customer, 1000)
        Invoice.objects.filter(pk=voided.pk).update(status=Invoice.Status.VOIDED)
        euro = issued_invoice(writer, customer, 1000, currency=Currency.CAD)
        proposal = propose("1:1", [make_tx(account, 1000, tag="a")], [engine_pick])
        many = propose(
            "N:1", [make_tx(account, 300, tag="b"), make_tx(account, 300, tag="c")], [engine_pick]
        )
        cases: list[tuple[MatchProposal, list[Invoice], type[Exception]]] = [
            (proposal, [], ValidationError),
            (proposal, [good, good], ValidationError),
            (proposal, [voided], InvalidTransition),
            (proposal, [euro], ValidationError),
            (many, [good, engine_pick], ValidationError),
        ]
        for target, chosen, error in cases:
            before = snapshot(target)
            with pytest.raises(error):
                reassign_proposal(writer, target, chosen)
            assert snapshot(target) == before

    def test_a_decided_proposal_and_missing_permissions_are_refused(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Only a pending proposal can be reassigned, by a principal with both write permissions.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        engine_pick = issued_invoice(writer, customer, 1000)
        chosen = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [engine_pick])
        for permission in (Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE):
            weak = Principal(writer.tenant_id, frozenset({permission}), user_id=writer.user_id)
            before = snapshot(proposal)
            with pytest.raises(PermissionDenied):
                reassign_proposal(weak, proposal, [chosen])
            assert snapshot(proposal) == before
        reject_proposal(writer, proposal)
        before = snapshot(proposal)
        with pytest.raises(InvalidTransition):
            reassign_proposal(writer, proposal, [chosen])
        assert snapshot(proposal) == before

    def test_an_invoice_held_by_another_pending_proposal_is_refused(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Runs treat invoices of pending proposals as held, and so does reassign.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        engine_pick = issued_invoice(writer, customer, 1000)
        held = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000, tag="a")], [engine_pick])
        propose("1:1", [make_tx(account, 1000, tag="b")], [held])
        before = snapshot(proposal)
        with pytest.raises(ValidationError):
            reassign_proposal(writer, proposal, [held])
        assert snapshot(proposal) == before

    def test_an_invoice_already_in_the_proposal_may_be_chosen(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Keeping an engine invoice, alone or with another one, is allowed.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        kept = due_invoice(writer, customer, 1000, 10)
        extra = due_invoice(writer, customer, 500, 20)
        alone = propose("1:1", [make_tx(account, 1000, tag="a")], [kept])
        assert len(reassign_proposal(writer, alone, [kept])) == 1
        assert status_of(kept) == Invoice.Status.PAID
        other = due_invoice(writer, customer, 700, 10)
        pair = propose("1:1", [make_tx(account, 1200, tag="b")], [other])
        assert len(reassign_proposal(writer, pair, [other, extra])) == 1
        assert MatchProposal.objects.get(pk=pair.pk).kind == "1:N"
        assert status_of(extra) == Invoice.Status.PAID
