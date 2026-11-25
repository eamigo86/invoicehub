"""Proposing matches: the run_reconciliation service and the reconcile_run task."""

from collections.abc import Generator
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import PermissionDenied

from accounts.models import User
from billing.models import Customer, Invoice
from billing.services import DraftLine, draft_credit_note, issue_invoice
from reconciliation import engine, services
from reconciliation.models import (
    BankAccount,
    BankTransaction,
    MatchProposal,
    MatchProposalInvoice,
    MatchProposalTransaction,
    ReconciliationRun,
)
from reconciliation.services import run_reconciliation
from reconciliation.tasks import reconcile_run
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice, make_tenant, pay
from tests.test_reconciliation_models import make_account, make_import

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant with issuer details and make it the active one.

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
    """Build a principal that may write invoices and reconciliation data.

    Args:
        tenant: Active tenant.
        user: User the principal stands for.

    Returns:
        The principal.
    """
    permissions = frozenset(
        {Permission.INVOICES_READ, Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE}
    )
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
def account(tenant: Tenant) -> BankAccount:
    """Create a bank account in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The account.
    """
    return make_account()


def make_tx(
    account: BankAccount, amount: int, description: str = "", payer: str = "Bolt", tag: str = "a"
) -> BankTransaction:
    """Create a bank transaction a few days after the invoices of the tests are issued.

    Args:
        account: Bank account of the transaction.
        amount: Signed amount in cents.
        description: Free text of the movement.
        payer: Payer name.
        tag: Character that makes the import hash and the dedupe hash unique.

    Returns:
        The saved transaction.
    """
    issued = Invoice.objects.filter(issue_date__isnull=False).first()
    assert issued is not None and issued.issue_date is not None
    statement = make_import(account, tag * 64)
    row: BankTransaction = BankTransaction.objects.create(
        bank_account=account,
        statement_import=statement,
        value_date=issued.issue_date + timedelta(days=3),
        amount=amount,
        description=description,
        payer=payer,
        dedupe_hash=tag * 64,
    )
    return row


def make_pending_proposal(
    tx: BankTransaction | None = None,
    invoice: Invoice | None = None,
    state: str = MatchProposal.State.PENDING,
) -> MatchProposal:
    """Create a proposal in a finished run, linked to the given rows.

    Args:
        tx: Transaction to link.
        invoice: Invoice to link.
        state: Review state of the proposal.

    Returns:
        The saved proposal.
    """
    run = ReconciliationRun.objects.create(config={}, status=ReconciliationRun.Status.COMPLETED)
    proposal: MatchProposal = MatchProposal.objects.create(
        run=run, kind="1:1", engine_status="auto", score=0.9, breakdown={}, residual=0, state=state
    )
    if tx:
        MatchProposalTransaction.objects.create(proposal=proposal, transaction=tx)
    if invoice:
        MatchProposalInvoice.objects.create(proposal=proposal, invoice=invoice)
    return proposal


def start(writer: Principal) -> ReconciliationRun:
    """Request a run and execute its task.

    Args:
        writer: Principal that may write reconciliation data.

    Returns:
        The run as stored after the task.
    """
    run = run_reconciliation(writer)
    reconcile_run(str(run.tenant_id), str(run.pk))
    stored: ReconciliationRun = ReconciliationRun.objects.get(pk=run.pk)
    return stored


class TestRunReconciliation:
    """The service that requests a run."""

    def test_needs_reconciliation_write(self, tenant: Tenant) -> None:
        """A principal without reconciliation:write, or none, is refused and nothing is stored.

        Args:
            tenant: Active tenant.
        """
        viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
        for principal in (viewer, None):
            with pytest.raises(PermissionDenied):
                run_reconciliation(principal)
        assert not ReconciliationRun.objects.exists()

    def test_creates_a_pending_run_and_enqueues_it_on_commit(
        self,
        writer: Principal,
        user: User,
        enqueued_reconcile: MagicMock,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        """The run stores config, engine version and requester, and is queued after commit.

        Args:
            writer: Principal that may write reconciliation data.
            user: User behind the principal.
            enqueued_reconcile: Mock standing in for the task delay.
            django_capture_on_commit_callbacks: Pytest-django fixture that runs commit hooks.
        """
        with django_capture_on_commit_callbacks(execute=True) as callbacks:  # type: ignore[operator]
            run = run_reconciliation(writer)
            enqueued_reconcile.assert_not_called()
        stored = ReconciliationRun.objects.get(pk=run.pk)
        assert stored.status == ReconciliationRun.Status.PENDING
        assert stored.config == engine.default_config()
        assert stored.engine_version == engine.engine_version()
        assert stored.requested_by_id == user.pk
        assert len(callbacks) == 1
        enqueued_reconcile.assert_called_once_with(str(stored.tenant_id), str(run.pk))

    def test_a_principal_without_a_user_leaves_the_requester_empty(self, tenant: Tenant) -> None:
        """A token or system principal has no user id, so requested_by stays null.

        Args:
            tenant: Active tenant.
        """
        system = Principal(tenant.pk, frozenset({Permission.RECONCILIATION_WRITE}))
        assert run_reconciliation(system).requested_by_id is None

    def test_a_pending_run_is_returned_and_nothing_is_enqueued(
        self,
        writer: Principal,
        enqueued_reconcile: MagicMock,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        """A second request while one is pending returns the same run and queues nothing.

        Args:
            writer: Principal that may write reconciliation data.
            enqueued_reconcile: Mock standing in for the task delay.
            django_capture_on_commit_callbacks: Pytest-django fixture that runs commit hooks.
        """
        first = run_reconciliation(writer)
        with django_capture_on_commit_callbacks(execute=True) as callbacks:  # type: ignore[operator]
            second = run_reconciliation(writer)
        assert second.pk == first.pk and not callbacks
        assert ReconciliationRun.objects.count() == 1
        enqueued_reconcile.assert_not_called()

    def test_a_finished_run_does_not_block_a_new_one(self, writer: Principal) -> None:
        """Only a pending run is unique, so a completed one is followed by a fresh run.

        Args:
            writer: Principal that may write reconciliation data.
        """
        first = run_reconciliation(writer)
        ReconciliationRun.objects.filter(pk=first.pk).update(status="completed")
        assert run_reconciliation(writer).pk != first.pk

    def test_losing_the_race_returns_the_winner(
        self, writer: Principal, monkeypatch: pytest.MonkeyPatch, enqueued_reconcile: MagicMock
    ) -> None:
        """When the pending check misses a run that appears first, its run is returned.

        Args:
            writer: Principal that may write reconciliation data.
            monkeypatch: Pytest monkeypatch fixture.
            enqueued_reconcile: Mock standing in for the task delay.
        """
        winner = ReconciliationRun.objects.create(config={})
        lookup = MagicMock(side_effect=[None, winner])
        monkeypatch.setattr(services, "_pending_run", lookup)
        assert run_reconciliation(writer).pk == winner.pk
        assert ReconciliationRun.objects.count() == 1
        enqueued_reconcile.assert_not_called()


class TestReconcileRun:
    """The task that proposes matches."""

    def test_an_exact_reference_is_an_auto_one_to_one(
        self, writer: Principal, customer: Customer, account: BankAccount, tenant: Tenant
    ) -> None:
        """A payment naming the invoice number for the exact amount is stored as an auto 1:1.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
            tenant: Active tenant.
        """
        invoice = issued_invoice(writer, customer, 1000)
        tx = make_tx(account, 1000, f"Payment {invoice.reference}")
        run = start(writer)
        proposal = MatchProposal.objects.get()
        assert (proposal.run_id, proposal.kind, proposal.engine_status) == (run.pk, "1:1", "auto")
        assert proposal.state == MatchProposal.State.PENDING and proposal.residual == 0
        assert proposal.breakdown["reference"] == 1.0 and 0 < proposal.score <= 1
        assert list(proposal.transactions.all()) == [tx]
        assert list(proposal.invoices.all()) == [invoice]
        link = MatchProposalTransaction.objects.get()
        assert link.tenant_id == tenant.pk
        assert MatchProposalInvoice.objects.get().tenant_id == tenant.pk

    def test_the_run_stores_counters_duration_and_completion(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Counters, timing and the completed status are filled, with the unmatched on each side.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        invoice = issued_invoice(writer, customer, 1000)
        issued_invoice(writer, customer, 2500)
        make_tx(account, 1000, f"Payment {invoice.reference}", tag="a")
        make_tx(account, 777, "Unknown", tag="b")
        make_tx(account, -300, "Coffee", tag="c")
        run = start(writer)
        assert run.status == ReconciliationRun.Status.COMPLETED and run.error == ""
        assert run.completed_at is not None and run.duration_ms is not None
        assert (run.transactions_considered, run.invoices_considered) == (2, 2)
        assert (run.auto_count, run.suggested_count, run.ambiguous_count) == (1, 0, 0)
        assert (run.unmatched_transactions, run.unmatched_invoices) == (1, 1)
        assert run.config == engine.default_config()
        assert run.engine_version == engine.engine_version()

    def test_a_debit_is_never_considered(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A negative amount is left out even when it names an invoice.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        make_tx(account, -1000, f"Refund {invoice.reference}")
        run = start(writer)
        assert run.transactions_considered == 0 and not MatchProposal.objects.exists()

    def test_the_balance_due_is_what_the_engine_gets(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A partly paid invoice is matched for what is left, and a settled one is left out.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        partly = issued_invoice(writer, customer, 1000)
        pay(writer, [(partly, 400)])
        settled = issued_invoice(writer, customer, 500)
        pay(writer, [(settled, 500)])
        make_tx(account, 600, f"Balance {partly.reference}")
        run = start(writer)
        assert run.invoices_considered == 1
        proposal = MatchProposal.objects.get()
        assert (proposal.engine_status, proposal.residual) == ("auto", 0)
        assert list(proposal.invoices.all()) == [partly]

    def test_credit_notes_and_drafts_are_not_open_invoices(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Only issued invoices with a payable status count, not drafts or credit notes.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        note = draft_credit_note(writer, invoice, [DraftLine("Credit", Decimal(1), 100)])
        issue_invoice(writer, note)
        Invoice.objects.create(customer=customer)
        make_tx(account, 900, f"Pay {invoice.reference}")
        run = start(writer)
        assert run.invoices_considered == 1
        assert MatchProposal.objects.get().engine_status == "auto"

    def test_a_transaction_in_a_pending_proposal_is_not_considered_again(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """Pending, applied and reassigned proposals hold their transactions, others free them.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transactions.
        """
        issued_invoice(writer, customer, 1000)
        held = [make_tx(account, 1000, "x", tag=t) for t in "abc"]
        free = make_tx(account, 1000, "x", tag="d")
        for tx, state in zip(held, ("pending", "applied", "reassigned"), strict=True):
            make_pending_proposal(tx=tx, state=state)
        make_pending_proposal(tx=free, state="rejected")
        run = start(writer)
        assert run.transactions_considered == 1

    def test_an_invoice_in_a_pending_proposal_is_excluded(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """An invoice already proposed and waiting for review is not offered again.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        held = issued_invoice(writer, customer, 1000)
        issued_invoice(writer, customer, 2000)
        make_pending_proposal(invoice=held)
        make_tx(account, 1000, f"Pay {held.reference}")
        run = start(writer)
        assert run.invoices_considered == 1
        assert run.transactions_considered == 1

    def test_running_the_task_twice_changes_nothing(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A second delivery of the task, or a delivery for a failed run, is a no-op.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        invoice = issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, f"Pay {invoice.reference}")
        run = start(writer)
        reconcile_run(str(run.tenant_id), str(run.pk))
        assert MatchProposal.objects.count() == 1
        assert ReconciliationRun.objects.get(pk=run.pk).completed_at == run.completed_at
        failed = ReconciliationRun.objects.create(config={}, status="failed", error="boom")
        reconcile_run(str(failed.tenant_id), str(failed.pk))
        assert ReconciliationRun.objects.get(pk=failed.pk).error == "boom"

    @pytest.mark.parametrize("error", [ValueError, TypeError, OverflowError])
    def test_an_engine_error_fails_the_run_without_proposals(
        self,
        writer: Principal,
        customer: Customer,
        account: BankAccount,
        monkeypatch: pytest.MonkeyPatch,
        error: type[Exception],
    ) -> None:
        """Any of the three engine errors marks the run failed and writes no proposal.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
            monkeypatch: Pytest monkeypatch fixture.
            error: Exception the engine raises.
        """
        invoice = issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, f"Pay {invoice.reference}")
        monkeypatch.setattr(engine, "match", MagicMock(side_effect=error("bad input")))
        run = start(writer)
        assert run.status == ReconciliationRun.Status.FAILED
        assert run.error == f"{error.__name__}: bad input"
        assert run.completed_at is not None and not MatchProposal.objects.exists()

    def test_same_amount_invoices_with_an_unrelated_payment_are_not_matched(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A payment naming no invoice number is not matched under the default config.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        issued_invoice(writer, customer, 1000)
        issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, "Thanks")
        run = start(writer)
        assert not MatchProposal.objects.exists()
        assert (run.unmatched_transactions, run.unmatched_invoices) == (1, 2)

    def test_a_payment_naming_both_invoices_is_stored_with_the_engine_status(
        self, writer: Principal, customer: Customer, account: BankAccount
    ) -> None:
        """A payment naming two same-amount invoices gets whatever the engine decides.

        Args:
            writer: Principal that may write reconciliation data.
            customer: Customer to invoice.
            account: Bank account of the transaction.
        """
        one = issued_invoice(writer, customer, 1000)
        two = issued_invoice(writer, customer, 1000)
        make_tx(account, 1000, f"{one.reference} {two.reference}")
        run = start(writer)
        statuses = {p.engine_status for p in MatchProposal.objects.all()}
        assert statuses == {"ambiguous"}, statuses
        assert run.ambiguous_count == MatchProposal.objects.count()
