"""Reconciliation runs and match proposals: engine wrapper, defaults, constraints and links."""

import json
from collections.abc import Generator

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from remitmatch import MatchConfig

from billing.models import Customer, Invoice
from reconciliation import engine
from reconciliation.models import (
    BankTransaction,
    MatchProposal,
    MatchProposalInvoice,
    MatchProposalTransaction,
    ReconciliationRun,
)
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tests.test_reconciliation_models import make_account, make_import, make_transaction

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant and make it the active one.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        yield tenant


def make_run() -> ReconciliationRun:
    """Create a run for the active tenant.

    Returns:
        The saved run.
    """
    run: ReconciliationRun = ReconciliationRun.objects.create(config=engine.default_config())
    return run


def make_proposal(run: ReconciliationRun | None = None, score: float = 0.95) -> MatchProposal:
    """Create a 1:1 auto proposal for the active tenant.

    Args:
        run: Run the proposal belongs to, a new one when omitted.
        score: Confidence of the proposal.

    Returns:
        The saved proposal.
    """
    proposal: MatchProposal = MatchProposal.objects.create(
        run=run or make_run(),
        kind=MatchProposal.Kind.ONE_TO_ONE,
        engine_status=MatchProposal.EngineStatus.AUTO,
        score=score,
        breakdown={"reference": 1.0},
        residual=0,
    )
    return proposal


def make_bank_transaction(hash_char: str = "h") -> BankTransaction:
    """Create a transaction of a new account for the active tenant.

    Args:
        hash_char: Character repeated to build the dedupe hash.

    Returns:
        The saved transaction.
    """
    account = make_account(f"bank-{hash_char}")
    return make_transaction(account, make_import(account, hash_char * 64), hash_char * 64)


def make_invoice() -> Invoice:
    """Create a draft invoice for the active tenant.

    Returns:
        The saved invoice.
    """
    invoice: Invoice = Invoice.objects.create(
        customer=Customer.objects.create(name="Acme"), currency="USD"
    )
    return invoice


def test_engine_version() -> None:
    """The wrapper reports the pinned remitmatch version."""
    assert engine.engine_version() == "0.1.0a2"


def test_default_config_matches_match_config_defaults() -> None:
    """The stored config equals the defaults, is JSON and rebuilds a MatchConfig."""
    config = engine.default_config()
    defaults = MatchConfig()
    assert config == {
        "amount_tolerance_minor": defaults.amount_tolerance_minor,
        "date_window_days": defaults.date_window_days,
        "max_group_size": defaults.max_group_size,
        "auto_threshold": defaults.auto_threshold,
        "suggest_threshold": defaults.suggest_threshold,
        "ambiguity_margin": defaults.ambiguity_margin,
        "weights": defaults.weights,
    }
    assert json.loads(json.dumps(config)) == config
    rebuilt = MatchConfig(**config)  # type: ignore[arg-type]
    assert rebuilt.weights == defaults.weights


def test_run_defaults(tenant: Tenant) -> None:
    """A new run is pending with zero counters and no completion data.

    Args:
        tenant: Active tenant.
    """
    run = make_run()
    run.refresh_from_db()
    assert run.status == ReconciliationRun.Status.PENDING
    assert (run.engine_version, run.error, run.requested_by) == ("", "", None)
    assert (run.duration_ms, run.completed_at) == (None, None)
    assert run.created_at is not None
    assert [
        run.transactions_considered,
        run.invoices_considered,
        run.auto_count,
        run.suggested_count,
        run.ambiguous_count,
        run.unmatched_transactions,
        run.unmatched_invoices,
    ] == [0] * 7


def test_runs_are_ordered_newest_first(tenant: Tenant) -> None:
    """The default ordering lists the latest run first.

    Args:
        tenant: Active tenant.
    """
    first = make_run()
    ReconciliationRun.objects.filter(pk=first.pk).update(status="completed")
    second = make_run()
    assert list(ReconciliationRun.objects.all()) == [second, first]


def test_proposal_defaults(tenant: Tenant) -> None:
    """A new proposal is pending, undecided and carries the engine values as given.

    Args:
        tenant: Active tenant.
    """
    proposal = make_proposal()
    proposal.refresh_from_db()
    assert proposal.state == MatchProposal.State.PENDING
    assert (proposal.decided_by, proposal.decided_at) == (None, None)
    assert (proposal.kind, proposal.engine_status) == ("1:1", "auto")
    assert (proposal.score, proposal.breakdown, proposal.residual) == (0.95, {"reference": 1.0}, 0)


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_score_outside_unit_interval_is_refused(tenant: Tenant, score: float) -> None:
    """A score below 0 or above 1 violates the check constraint.

    Args:
        tenant: Active tenant.
        score: The invalid score.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        make_proposal(score=score)


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_score_bounds_are_allowed(tenant: Tenant, score: float) -> None:
    """The bounds 0 and 1 are valid scores.

    Args:
        tenant: Active tenant.
        score: The boundary score.
    """
    assert make_proposal(score=score).pk


def test_proposal_links_transactions_and_invoices(tenant: Tenant) -> None:
    """Transactions and invoices added to a proposal are read back from both sides.

    Args:
        tenant: Active tenant.
    """
    proposal = make_proposal()
    bank_tx, invoice = make_bank_transaction(), make_invoice()
    MatchProposalTransaction.objects.create(proposal=proposal, transaction=bank_tx)
    MatchProposalInvoice.objects.create(proposal=proposal, invoice=invoice)
    assert list(proposal.transactions.all()) == [bank_tx]
    assert list(proposal.invoices.all()) == [invoice]
    assert list(bank_tx.proposals.all()) == [proposal]
    assert proposal.run.proposals.get() == proposal


def test_link_pairs_are_unique_per_tenant(tenant: Tenant) -> None:
    """The same proposal link is refused twice in one tenant and allowed in another.

    Args:
        tenant: Active tenant.
    """
    proposal, bank_tx, invoice = make_proposal(), make_bank_transaction(), make_invoice()
    MatchProposalTransaction.objects.create(proposal=proposal, transaction=bank_tx)
    MatchProposalInvoice.objects.create(proposal=proposal, invoice=invoice)
    with pytest.raises(IntegrityError), transaction.atomic():
        MatchProposalTransaction.objects.create(proposal=proposal, transaction=bank_tx)
    with pytest.raises(IntegrityError), transaction.atomic():
        MatchProposalInvoice.objects.create(proposal=proposal, invoice=invoice)
    other = Tenant.objects.create(name="B")
    with tenant_context(other.pk):
        other_proposal = make_proposal()
        assert MatchProposalTransaction.objects.create(
            proposal=other_proposal, transaction=make_bank_transaction()
        ).pk
        assert MatchProposalInvoice.objects.create(
            proposal=other_proposal, invoice=make_invoice()
        ).pk


def test_deleting_a_proposal_cascades_links_but_a_linked_transaction_is_protected(
    tenant: Tenant,
) -> None:
    """Links die with their proposal; a transaction or invoice in a link cannot be deleted.

    Args:
        tenant: Active tenant.
    """
    proposal, bank_tx, invoice = make_proposal(), make_bank_transaction(), make_invoice()
    MatchProposalTransaction.objects.create(proposal=proposal, transaction=bank_tx)
    MatchProposalInvoice.objects.create(proposal=proposal, invoice=invoice)
    with pytest.raises(ProtectedError), transaction.atomic():
        bank_tx.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        invoice.delete()
    proposal.delete()
    assert not MatchProposalTransaction.objects.exists()
    assert not MatchProposalInvoice.objects.exists()
    assert BankTransaction.objects.filter(pk=bank_tx.pk).exists()
