"""Applying proposals: bank payments created by auto matches and by the shared helper."""

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from billing import services as billing_services
from billing.models import AuditEvent, Customer, Invoice, Payment
from billing.services import (
    DraftLine,
    InvalidTransition,
    draft_credit_note,
    issue_invoice,
    save_draft,
)
from reconciliation import engine
from reconciliation.models import (
    BankAccount,
    BankTransaction,
    MatchProposal,
    MatchProposalInvoice,
    MatchProposalTransaction,
    ReconciliationRun,
)
from reconciliation.services import _apply_proposal
from reconciliation.tasks import reconcile_run
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice, pay
from tests.test_reconciliation_engine import make_tx, start

pytestmark = pytest.mark.django_db

EXACT = {"reference": 1.0, "amount": 1.0, "name": 1.0, "date": 1.0}


@pytest.fixture
def system(tenant: Any) -> Principal:
    """Build the principal of automatic work: permissions but no user and no token.

    Args:
        tenant: Active tenant.

    Returns:
        The principal.
    """
    return Principal(
        tenant.pk, frozenset({Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE})
    )


def propose(
    kind: str,
    txs: list[BankTransaction],
    invoices: list[Invoice],
    breakdown: dict[str, float] | None = None,
    status: str = "auto",
) -> MatchProposal:
    """Build a pending proposal by hand, for shapes the engine does not easily produce.

    Args:
        kind: Cardinality of the proposal.
        txs: Transactions to link.
        invoices: Invoices to link.
        breakdown: Score per signal, exact by default.
        status: Engine status of the proposal.

    Returns:
        The saved proposal.
    """
    run = ReconciliationRun.objects.create(config={}, status=ReconciliationRun.Status.COMPLETED)
    proposal: MatchProposal = MatchProposal.objects.create(
        run=run,
        kind=kind,
        engine_status=status,
        score=0.9,
        breakdown=breakdown or EXACT,
        residual=0,
    )
    for tx in txs:
        MatchProposalTransaction.objects.create(proposal=proposal, transaction=tx)
    for invoice in invoices:
        MatchProposalInvoice.objects.create(proposal=proposal, invoice=invoice)
    return proposal


def due_invoice(writer: Principal, customer: Customer, cents: int, days: int) -> Invoice:
    """Draft and issue an invoice that falls due a given number of days from today.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        cents: Price of the single line, which is also the total.
        days: Days from today until the due date.

    Returns:
        The issued invoice.
    """
    draft = save_draft(
        writer,
        Invoice(customer=customer, due_date=date.today() + timedelta(days=days)),
        [DraftLine("Widget", Decimal(1), cents)],
    )
    return issue_invoice(writer, draft)


def status_of(invoice: Invoice) -> str:
    """Read the stored status of an invoice.

    Args:
        invoice: Invoice to reload.

    Returns:
        Its status.
    """
    return str(Invoice.objects.get(pk=invoice.pk).status)


class TestAutoApplication:
    """The task applies auto matches that name the invoice number exactly."""

    def test_an_exact_reference_auto_match_is_applied(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """One bank payment, a paid invoice, the link, an applied proposal and a system event.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000, f"Payment {invoice.reference}")
        run = start(writer)
        proposal = MatchProposal.objects.get()
        payment = Payment.objects.get()
        assert (payment.source, payment.amount, payment.currency) == ("bank", 1000, "USD")
        assert payment.value_date == tx.value_date and payment.reference == tx.description
        assert status_of(invoice) == Invoice.Status.PAID
        assert BankTransaction.objects.get(pk=tx.pk).payment_id == payment.pk
        assert proposal.state == MatchProposal.State.APPLIED
        assert proposal.decided_by_id is None and proposal.decided_at is not None
        event = AuditEvent.objects.get(action="reconciliation.proposal_applied")
        assert event.entity_id == proposal.pk
        assert event.actor_kind == AuditEvent.ActorKind.SYSTEM
        assert event.before == {"state": "pending"}
        assert event.after == {"state": "applied", "payment_ids": [payment.pk]}
        assert run.applied_count == 1

    def test_an_auto_match_without_an_exact_reference_stays_pending(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Alpha rule: accuracy was measured on synthetic data, so only reference 1.0 applies.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
            monkeypatch: Pytest monkeypatch fixture.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000, f"Payment {invoice.reference}")
        match = SimpleNamespace(
            kind=SimpleNamespace(value="1:1"),
            status=SimpleNamespace(value="auto"),
            score=0.95,
            breakdown={**EXACT, "reference": 0.9},
            residual_minor=0,
            payment_ids=(str(tx.pk),),
            invoice_ids=(str(invoice.pk),),
        )
        result = SimpleNamespace(matches=[match], unmatched_payments=[], unmatched_invoices=[])
        monkeypatch.setattr(engine, "match", lambda *args, **kwargs: result)
        run = start(writer)
        assert MatchProposal.objects.get().state == MatchProposal.State.PENDING
        assert not Payment.objects.exists() and run.applied_count == 0

    def test_an_ambiguous_match_stays_pending(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A payment naming two same-amount invoices is ambiguous and waits for a person.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        first = issued_invoice(writer, customer, 1000)
        second = issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, f"Pay {first.reference} {second.reference}")
        run = start(writer)
        proposal = MatchProposal.objects.get()
        assert proposal.engine_status == "ambiguous" and proposal.breakdown["reference"] == 1.0
        assert proposal.state == MatchProposal.State.PENDING
        assert not Payment.objects.exists() and run.applied_count == 0

    def test_a_proposal_that_fails_stays_pending_and_the_others_apply(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An invoice voided after matching refuses its proposal without stopping the run.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
            monkeypatch: Pytest monkeypatch fixture.
        """
        gone = issued_invoice(writer, customer, 1000)
        fine = issued_invoice(writer, customer, 2000)
        gone_tx = make_tx(account, 1000, f"Pay {gone.reference}", tag="a")
        fine_tx = make_tx(account, 2000, f"Pay {fine.reference}", tag="b")
        real_match = engine.match

        def match_then_void(*args: Any, **kwargs: Any) -> Any:
            result = real_match(*args, **kwargs)
            Invoice.objects.filter(pk=gone.pk).update(status=Invoice.Status.VOIDED)
            return result

        monkeypatch.setattr(engine, "match", match_then_void)
        run = start(writer)
        assert run.applied_count == 1 and run.auto_count == 2
        assert BankTransaction.objects.get(pk=gone_tx.pk).payment_id is None
        assert BankTransaction.objects.get(pk=fine_tx.pk).payment_id is not None
        assert Payment.objects.count() == 1 and status_of(fine) == Invoice.Status.PAID
        states = {p.transactions.get().pk: p.state for p in MatchProposal.objects.all()}
        assert states == {gone_tx.pk: "pending", fine_tx.pk: "applied"}

    def test_an_unexpected_error_leaves_that_proposal_pending_and_the_run_completes(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any exception from one proposal is contained: no wedged run, the others apply.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
            monkeypatch: Pytest monkeypatch fixture.
        """
        broken = issued_invoice(writer, customer, 1000)
        fine = issued_invoice(writer, customer, 2000)
        broken_tx = make_tx(account, 1000, f"Pay {broken.reference}", tag="a")
        fine_tx = make_tx(account, 2000, f"Pay {fine.reference}", tag="b")
        real = _apply_proposal

        def explode(principal: Principal, proposal: MatchProposal) -> list[Payment]:
            if proposal.transactions.filter(pk=broken_tx.pk).exists():
                raise RuntimeError("boom")
            return real(principal, proposal)

        monkeypatch.setattr("reconciliation.tasks._apply_proposal", explode)
        run = start(writer)
        assert run.status == ReconciliationRun.Status.COMPLETED and run.applied_count == 1
        assert BankTransaction.objects.get(pk=broken_tx.pk).payment_id is None
        assert BankTransaction.objects.get(pk=fine_tx.pk).payment_id is not None
        assert MatchProposal.objects.get(transactions=broken_tx).state == "pending"
        assert ReconciliationRun.objects.filter(status="pending").count() == 0

    def test_an_auto_application_is_attributed_to_the_system(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Proposal, proposal event and payment events name no user and no token.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, f"Pay {invoice.reference}")
        start(writer)
        assert MatchProposal.objects.get().decided_by_id is None
        events = AuditEvent.objects.filter(
            action__in=["reconciliation.proposal_applied", "invoice.payment_recorded"]
        )
        assert {e.action for e in events} == {
            "reconciliation.proposal_applied",
            "invoice.payment_recorded",
        }
        for event in events:
            assert event.actor_kind == AuditEvent.ActorKind.SYSTEM
            assert event.actor_user_id is None and event.actor_token_id is None

    def test_a_second_run_applies_nothing_twice(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """The first run applied the match, so the next run finds nothing to propose or apply.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, f"Pay {invoice.reference}")
        first = start(writer)
        reconcile_run(str(first.tenant_id), str(first.pk))
        second = start(writer)
        assert (first.applied_count, second.applied_count) == (1, 0)
        assert second.transactions_considered == 0
        assert Payment.objects.count() == 1 and MatchProposal.objects.count() == 1
        assert AuditEvent.objects.filter(action="reconciliation.proposal_applied").count() == 1

    def test_a_transaction_with_a_payment_is_not_considered(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A transaction that already created a payment is left out even with no proposal.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000, f"Pay {invoice.reference}")
        paid = Payment.objects.create(amount=1000, currency="USD", value_date=tx.value_date)
        BankTransaction.objects.filter(pk=tx.pk).update(payment=paid)
        run = start(writer)
        assert run.transactions_considered == 0 and not MatchProposal.objects.exists()


def test_a_payment_is_linked_to_one_transaction_only(
    writer: Principal,
    customer: Customer,
    account: BankAccount,
) -> None:
    """Two transactions cannot claim the same payment.

    Args:
        writer: Principal that may write invoices.
        customer: Customer to invoice.
        account: Bank account of the transactions.
    """
    issued_invoice(writer, customer, 1000)
    first, second = make_tx(account, 400, tag="a"), make_tx(account, 400, tag="b")
    payment = Payment.objects.create(amount=400, currency="USD", value_date=first.value_date)
    BankTransaction.objects.filter(pk=first.pk).update(payment=payment)
    with pytest.raises(IntegrityError), transaction.atomic():
        BankTransaction.objects.filter(pk=second.pk).update(payment=payment)


class TestApplyProposal:
    """The shared helper that turns a pending proposal into bank payments."""

    def test_a_partial_amount_leaves_the_invoice_partially_paid(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A transaction below the balance pays part of the invoice.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 400)
        payments = _apply_proposal(system, propose("1:1", [tx], [invoice]))
        assert [p.amount for p in payments] == [400]
        assert status_of(invoice) == Invoice.Status.PARTIALLY_PAID
        assert invoice.balance_due() == 600

    def test_one_to_many_allocates_in_due_date_order(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """One payment covers the earliest due invoice first, then the next.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        late = due_invoice(writer, customer, 1000, 60)
        early = due_invoice(writer, customer, 500, 10)
        tx = make_tx(account, 1200)
        payments = _apply_proposal(system, propose("1:N", [tx], [late, early]))
        assert len(payments) == 1
        allocated = {a.invoice_id: a.amount for a in payments[0].allocations.all()}
        assert allocated == {early.pk: 500, late.pk: 700}
        assert status_of(early) == Invoice.Status.PAID
        assert status_of(late) == Invoice.Status.PARTIALLY_PAID

    def test_many_to_one_records_a_payment_per_transaction(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """Each transaction becomes its own payment, in value date order, and the invoice is paid.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        invoice = issued_invoice(writer, customer, 1000)
        first = make_tx(account, 600, tag="a")
        second = make_tx(account, 400, tag="b")
        payments = _apply_proposal(system, propose("N:1", [second, first], [invoice]))
        assert sorted(p.amount for p in payments) == [400, 600]
        assert Payment.objects.count() == 2 and status_of(invoice) == Invoice.Status.PAID
        for tx in (first, second):
            assert BankTransaction.objects.get(pk=tx.pk).payment is not None

    def test_an_overpayment_is_capped_and_the_rest_stays_unallocated(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """The allocation never exceeds the balance; the payment keeps its full amount.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1500)
        (payment,) = _apply_proposal(system, propose("1:1", [tx], [invoice]))
        assert payment.amount == 1500
        assert [a.amount for a in payment.allocations.all()] == [1000]
        assert status_of(invoice) == Invoice.Status.PAID

    def test_a_transaction_with_nothing_left_to_allocate_still_gets_a_payment(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """Bank money beyond the invoice is recorded unallocated and its transaction is linked.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        invoice = issued_invoice(writer, customer, 1000)
        txs = [make_tx(account, 600, tag="a"), make_tx(account, 600, tag="b")]
        txs.append(make_tx(account, 100, tag="c"))
        for days, tx in enumerate(txs):
            BankTransaction.objects.filter(pk=tx.pk).update(
                value_date=tx.value_date + timedelta(days=days)
            )
        payments = _apply_proposal(system, propose("N:1", txs, [invoice]))
        assert [a.amount for p in payments for a in p.allocations.all()] == [600, 400]
        assert sorted(p.amount for p in payments) == [100, 600, 600]
        assert all(BankTransaction.objects.get(pk=t.pk).payment_id for t in txs)

    def test_the_decision_and_audit_event_are_recorded(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        user: Any,
    ) -> None:
        """A user principal becomes decided_by and the actor of the proposal event.

        Args:
            writer: Principal that may write invoices and reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
            user: User behind the principal.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        (payment,) = _apply_proposal(writer, proposal)
        stored = MatchProposal.objects.get(pk=proposal.pk)
        assert stored.state == "applied" and stored.decided_by_id == user.pk
        event = AuditEvent.objects.get(action="reconciliation.proposal_applied")
        assert (event.actor_kind, event.actor_user_id) == (AuditEvent.ActorKind.USER, user.pk)
        assert event.after == {"state": "applied", "payment_ids": [payment.pk]}

    def test_an_applied_proposal_cannot_be_applied_again(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """Only a pending proposal applies, so a second call changes nothing.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 400)], [invoice])
        _apply_proposal(system, proposal)
        with pytest.raises(InvalidTransition):
            _apply_proposal(system, proposal)
        assert Payment.objects.count() == 1

    def test_a_transaction_that_already_has_a_payment_is_refused(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A transaction never creates two payments, and nothing is written on refusal.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 400)
        paid = Payment.objects.create(amount=400, currency="USD", value_date=tx.value_date)
        BankTransaction.objects.filter(pk=tx.pk).update(payment=paid)
        proposal = propose("1:1", [tx], [invoice])
        with pytest.raises(InvalidTransition):
            _apply_proposal(system, proposal)
        assert Payment.objects.count() == 1 and status_of(invoice) == Invoice.Status.ISSUED
        assert MatchProposal.objects.get(pk=proposal.pk).state == "pending"

    def test_a_voided_invoice_refuses_the_proposal_and_writes_nothing(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """The proposal stays pending with no payment, link or audit event.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000)
        proposal = propose("1:1", [tx], [invoice])
        Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.VOIDED)
        with pytest.raises(InvalidTransition), transaction.atomic():
            _apply_proposal(system, proposal)
        assert not Payment.objects.exists()
        assert BankTransaction.objects.get(pk=tx.pk).payment_id is None
        assert MatchProposal.objects.get(pk=proposal.pk).state == "pending"
        assert not AuditEvent.objects.filter(action="reconciliation.proposal_applied").exists()

    def test_an_invoice_paid_after_matching_is_refused(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A settled invoice is not payable, so no unallocated payment is recorded for it.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000)
        proposal = propose("1:1", [tx], [invoice])
        pay(writer, [(invoice, 1000)])
        with pytest.raises(InvalidTransition):
            _apply_proposal(system, proposal)
        assert Payment.objects.count() == 1
        assert BankTransaction.objects.get(pk=tx.pk).payment_id is None
        assert MatchProposal.objects.get(pk=proposal.pk).state == "pending"

    def test_an_invoice_with_no_balance_left_is_refused(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A payable status does not help when the balance due is already zero.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        pay(writer, [(invoice, 1000)])
        Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.SENT)
        with pytest.raises(InvalidTransition):
            _apply_proposal(system, proposal)
        assert Payment.objects.count() == 1
        assert MatchProposal.objects.get(pk=proposal.pk).state == "pending"

    def test_an_invoice_credited_in_full_after_matching_is_refused(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A credit note that brings the balance to zero blocks the application.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        issue_invoice(
            writer, draft_credit_note(writer, invoice, [DraftLine("C", Decimal(1), 1000)])
        )
        with pytest.raises(InvalidTransition):
            _apply_proposal(system, proposal)
        assert not Payment.objects.exists()
        assert MatchProposal.objects.get(pk=proposal.pk).state == "pending"

    def test_a_failure_after_the_first_payment_rolls_the_first_back(
        self,
        system: Principal,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The savepoint undoes payments already recorded when a later one fails.

        Args:
            system: Principal of automatic work.
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transactions.
            monkeypatch: Pytest monkeypatch fixture.
        """
        invoice = issued_invoice(writer, customer, 1000)
        txs = [make_tx(account, 600, tag="a"), make_tx(account, 400, tag="b")]
        proposal = propose("N:1", txs, [invoice])
        real: Callable[..., Payment] = billing_services.record_payment
        calls: list[int] = []

        def flaky(*args: Any) -> Payment:
            calls.append(1)
            if len(calls) == 2:
                raise ValidationError("boom")
            return real(*args)

        monkeypatch.setattr("reconciliation.services.record_payment", flaky)
        with pytest.raises(ValidationError):
            _apply_proposal(system, proposal)
        assert not Payment.objects.exists() and status_of(invoice) == Invoice.Status.ISSUED
        assert MatchProposal.objects.get(pk=proposal.pk).state == "pending"

    def test_it_needs_reconciliation_write(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
    ) -> None:
        """A principal that can only write invoices, or none, is refused.

        Args:
            writer: Principal that may write invoices.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        proposal = propose("1:1", [make_tx(account, 1000)], [invoice])
        invoices_only = Principal(writer.tenant_id, frozenset({Permission.INVOICES_WRITE}))
        for principal in (invoices_only, None):
            with pytest.raises(PermissionDenied):
                _apply_proposal(principal, proposal)
        assert not Payment.objects.exists()
