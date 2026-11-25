"""Reconciliation models: defaults, constraints per tenant, upload path and display."""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from reconciliation.models import BankAccount, BankStatementImport, BankTransaction
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def make_tenant(name: str) -> Tenant:
    """Create a tenant.

    Args:
        name: Name of the tenant.

    Returns:
        The tenant.
    """
    return Tenant.objects.create(name=name)


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant and make it the active one.

    Yields:
        The tenant.
    """
    tenant = make_tenant("A")
    with tenant_context(tenant.pk):
        yield tenant


def make_account(name: str = "Chase") -> BankAccount:
    """Create a bank account for the active tenant.

    Args:
        name: Name of the account.

    Returns:
        The saved account.
    """
    account: BankAccount = BankAccount.objects.create(
        name=name,
        account_identifier="1234",
        date_column="Date",
        amount_column="Amount",
        description_column="Memo",
    )
    return account


def make_import(account: BankAccount, sha: str = "a" * 64) -> BankStatementImport:
    """Create a statement import for the active tenant.

    Args:
        account: Bank account the statement belongs to.
        sha: SHA-256 of the file.

    Returns:
        The saved import.
    """
    statement: BankStatementImport = BankStatementImport.objects.create(
        bank_account=account,
        file=ContentFile(b"a,b\n", name="x.csv"),
        file_sha256=sha,
        mapping={},
    )
    return statement


def make_transaction(
    account: BankAccount, statement: BankStatementImport, dedupe_hash: str = "h" * 64
) -> BankTransaction:
    """Create a bank transaction for the active tenant.

    Args:
        account: Bank account of the transaction.
        statement: Import that brought the transaction in.
        dedupe_hash: Deduplication hash.

    Returns:
        The saved transaction.
    """
    row: BankTransaction = BankTransaction.objects.create(
        bank_account=account,
        statement_import=statement,
        value_date=date(2026, 6, 15),
        amount=-1250,
        description="Coffee",
        dedupe_hash=dedupe_hash,
    )
    return row


def test_bank_account_defaults_and_str(tenant: Tenant) -> None:
    """A new account uses USD and the US date format, and displays its name.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    assert (account.currency, account.date_format, account.payer_column) == (
        "USD",
        "%m/%d/%Y",
        "",
    )
    assert str(account) == "Chase"


def test_statement_import_defaults_and_upload_path(tenant: Tenant) -> None:
    """A new import is pending with zero counters and stores the file under its pk.

    Args:
        tenant: Active tenant.
    """
    statement = make_import(make_account())
    assert statement.status == BankStatementImport.Status.PENDING
    assert (statement.rows_read, statement.duplicates, statement.imported) == (0, 0, 0)
    assert (statement.error, statement.completed_at) == ("", None)
    assert statement.created_at is not None
    assert statement.file.name == f"tenants/{tenant.pk}/bank_imports/{statement.pk}.csv"


def test_transaction_keeps_signed_integer_cents(tenant: Tenant) -> None:
    """A debit is stored as a negative integer and the payer is optional.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    transaction_ = make_transaction(account, make_import(account))
    transaction_.refresh_from_db()
    assert (transaction_.amount, transaction_.payer) == (-1250, "")


def test_account_name_is_unique_per_tenant(tenant: Tenant) -> None:
    """The same name is refused twice in one tenant and allowed in another.

    Args:
        tenant: Active tenant.
    """
    make_account()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_account()
    other = make_tenant("B")
    with tenant_context(other.pk):
        assert make_account().pk


def test_import_is_unique_per_account_and_hash(tenant: Tenant) -> None:
    """The same file hash is refused twice for one account, allowed for another tenant.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    make_import(account)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_import(account)
    other = make_tenant("B")
    with tenant_context(other.pk):
        other_account = make_account()
        assert make_import(other_account).pk


def test_transaction_hash_is_unique_per_tenant(tenant: Tenant) -> None:
    """The same dedupe hash is refused twice in one tenant and allowed in another.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    statement = make_import(account)
    make_transaction(account, statement)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_transaction(account, statement)
    other = make_tenant("B")
    with tenant_context(other.pk):
        other_account = make_account()
        assert make_transaction(other_account, make_import(other_account)).pk


def test_models_use_uuid_primary_keys(tenant: Tenant) -> None:
    """Every model gets its uuid before insert.

    Args:
        tenant: Active tenant.
    """
    for model in (BankAccount, BankStatementImport, BankTransaction):
        assert isinstance(model._meta.pk.get_default(), uuid.UUID)
