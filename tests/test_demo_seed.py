"""The demo seed: the demo user, the statement it builds and how the engine reads it."""

from unittest.mock import MagicMock

import pytest
from allauth.account.models import EmailAddress
from django.db.models import Q

from accounts.models import User
from billing.models import Invoice
from reconciliation import seed
from reconciliation.models import (
    BankAccount,
    BankStatementImport,
    BankTransaction,
    MatchProposal,
    ReconciliationRun,
)
from reconciliation.parsing import parse_statement
from reconciliation.seed import DEMO_COMPANY, DEMO_EMAIL, DEMO_PASSWORD, RETAINER_CENTS, seed_demo
from reconciliation.services import run_reconciliation
from reconciliation.tasks import import_statement, reconcile_run
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Principal

pytestmark = pytest.mark.django_db

MAPPING = {
    "date_column": "Date",
    "amount_column": "Amount",
    "description_column": "Description",
    "payer_column": "Payer",
    "date_format": "%m/%d/%Y",
}


def test_the_demo_user_owns_a_tenant_and_can_open_the_admin() -> None:
    """The demo user is a staff superuser with a verified primary email who owns the company."""
    demo = seed_demo()
    user = User.objects.get(pk=demo.user_id)
    assert user.is_staff and user.is_superuser
    assert user.check_password(DEMO_PASSWORD)
    assert EmailAddress.objects.filter(
        user=user, email=DEMO_EMAIL, primary=True, verified=True
    ).exists()
    membership = Membership.objects.get(user=user)
    assert membership.role == Membership.Role.OWNER
    assert membership.tenant.name == DEMO_COMPANY
    assert membership.tenant_id == demo.tenant_id


def test_the_statement_has_a_row_for_every_story() -> None:
    """The CSV holds four exact rows, a typo, the retainer pair, two credits and one debit."""
    demo = seed_demo()
    rows = parse_statement(demo.statement, MAPPING, "demo")
    assert len(rows) == 9
    with tenant_context(demo.tenant_id):
        pair = list(Invoice.objects.filter(total=RETAINER_CENTS, status=Invoice.Status.ISSUED))
        open_invoices = {
            invoice.reference: (invoice.balance_due(), invoice.customer_details["name"])
            for invoice in Invoice.objects.filter(
                ~Q(total=RETAINER_CENTS),
                kind=Invoice.Kind.INVOICE,
                status__in=Invoice.PAYABLE_STATUSES,
            )
            if invoice.balance_due() > 0
        }
    exact = [r for r in rows if r.description.removeprefix("ACH PAYMENT ") in open_invoices]
    assert len(exact) == 4
    for row in exact:
        assert (row.amount, row.payer) == open_invoices[
            row.description.removeprefix("ACH PAYMENT ")
        ]
    assert len(pair) == 2 and pair[0].customer_id == pair[1].customer_id
    [pair_row] = [r for r in rows if r.amount == RETAINER_CENTS]
    assert all(invoice.reference in pair_row.description for invoice in pair)
    assert pair_row.payer == pair[0].customer_details["name"]
    others = [r for r in rows if r not in [*exact, pair_row]]
    assert sorted((r.description, r.amount) for r in others if r.amount < 0) == [
        ("MONTHLY SERVICE FEE", -2_500)
    ]
    credits = {r.description: r for r in others if r.amount > 0}
    assert credits["STRIPE TRANSFER"].amount == 231_840
    assert credits["INTEREST PAYMENT"].amount == 87
    typo = next(r for r in credits.values() if r.description.startswith("ACH PAYMENT INV-"))
    assert typo.amount > 0 and len(others) == 4


def test_reconciling_the_demo_gives_every_outcome(
    enqueued_import: MagicMock,
    enqueued_reconcile: MagicMock,
    django_capture_on_commit_callbacks: object,
) -> None:
    """Auto, suggested, ambiguous and unmatched results all come out of the demo statement.

    Args:
        enqueued_import: Mock standing in for the import task delay.
        enqueued_reconcile: Mock standing in for the run task delay.
        django_capture_on_commit_callbacks: Fixture that runs on-commit callbacks.
    """
    enqueued_import.side_effect = import_statement
    enqueued_reconcile.side_effect = reconcile_run
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        demo = seed_demo()
    with tenant_context(demo.tenant_id):
        principal = Principal.from_membership(Membership.objects.get(user_id=demo.user_id))
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            run_reconciliation(principal)
        statement = BankStatementImport.objects.get(pk=demo.statement_import_id)
        run = ReconciliationRun.objects.get()
        counters = (
            run.auto_count,
            run.applied_count,
            run.suggested_count,
            run.ambiguous_count,
            run.unmatched_transactions,
        )
        pending = list(
            MatchProposal.objects.filter(state=MatchProposal.State.PENDING).values_list(
                "engine_status", flat=True
            )
        )
        paid = Invoice.objects.filter(status=Invoice.Status.PAID).count()
    assert (statement.status, statement.rows_read) == (BankStatementImport.Status.COMPLETED, 9)
    assert counters == (4, 4, 1, 1, 2)
    assert sorted(pending) == ["ambiguous", "suggested"]
    assert paid == 4 + 2  # the four matched ones, plus the two the billing seed already paid


def test_a_failure_leaves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the import cannot be created, no user, tenant or email is left behind.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """

    def boom(*args: object, **kwargs: object) -> None:
        """Fail like a broken storage would.

        Args:
            *args: Ignored positional arguments.
            **kwargs: Ignored keyword arguments.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("storage down")

    monkeypatch.setattr(seed, "create_import", boom)
    with pytest.raises(RuntimeError):
        seed_demo()
    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert not EmailAddress.objects.exists()


@pytest.mark.isolation
def test_another_tenant_sees_none_of_the_demo(
    enqueued_import: MagicMock, django_capture_on_commit_callbacks: object
) -> None:
    """Invoices, accounts, imports and transactions of the demo are invisible to others.

    Args:
        enqueued_import: Mock standing in for the import task delay.
        django_capture_on_commit_callbacks: Fixture that runs on-commit callbacks.
    """
    enqueued_import.side_effect = import_statement
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        demo = seed_demo()
    models = (Invoice, BankAccount, BankStatementImport, BankTransaction)
    with tenant_context(demo.tenant_id):
        assert all(model.objects.exists() for model in models)
    other = Tenant.objects.create(name="Other")
    with tenant_context(other.pk):
        assert not any(model.objects.exists() for model in models)
