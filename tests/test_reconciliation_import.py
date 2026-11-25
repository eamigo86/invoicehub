"""Importing bank statements: the create_import service and the import_statement task."""

import hashlib
from collections.abc import Generator
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from reconciliation.models import BankAccount, BankStatementImport, BankTransaction
from reconciliation.services import create_import
from reconciliation.tasks import import_statement
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db

HEADER = "Date,Amount,Memo,From\n"
RENT = "01/31/2026,1000.00,Rent,Bob\n"
COFFEE = "02/01/2026,-4.50,Coffee,\n"
TAX = "02/02/2026,-20.00,Tax,\n"


def csv(*rows: str) -> bytes:
    """Build a statement in the account layout.

    Args:
        *rows: Data lines, each ending with a newline.

    Returns:
        The file content.
    """
    return (HEADER + "".join(rows)).encode()


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant and make it the active one.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        yield tenant


def writer(tenant: Tenant) -> Principal:
    """Build a principal who may write reconciliation data.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, frozenset({Permission.RECONCILIATION_WRITE}))


def make_account() -> BankAccount:
    """Create a bank account with the layout of the test files.

    Returns:
        The saved account.
    """
    account: BankAccount = BankAccount.objects.create(
        name="Chase",
        account_identifier="1234",
        date_column="Date",
        amount_column="Amount",
        description_column="Memo",
        payer_column="From",
    )
    return account


def test_create_import_needs_reconciliation_write(tenant: Tenant) -> None:
    """A principal without reconciliation:write is refused and nothing is stored.

    Args:
        tenant: Active tenant.
    """
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        create_import(viewer, make_account(), csv(RENT))
    assert not BankStatementImport.objects.exists()


def test_create_import_snapshots_the_mapping(tenant: Tenant) -> None:
    """The import is pending and keeps the account columns as they were at upload.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    row = create_import(writer(tenant), account, csv(RENT))
    account.date_column = "Booked"
    account.save()
    stored = BankStatementImport.objects.get(pk=row.pk)
    assert stored.mapping == {
        "date_column": "Date",
        "amount_column": "Amount",
        "description_column": "Memo",
        "payer_column": "From",
        "date_format": "%m/%d/%Y",
    }
    assert (stored.status, stored.bank_account_id) == ("pending", account.pk)
    assert stored.file_sha256 == hashlib.sha256(csv(RENT)).hexdigest()


def test_create_import_stores_the_file_under_the_tenant(tenant: Tenant, media_root: Path) -> None:
    """The file is written at the tenant path with the uploaded bytes.

    Args:
        tenant: Active tenant.
        media_root: Temporary media directory.
    """
    row = create_import(writer(tenant), make_account(), csv(RENT))
    path = f"tenants/{tenant.pk}/bank_imports/{row.pk}.csv"
    assert BankStatementImport.objects.get(pk=row.pk).file.name == path
    assert (media_root / path).read_bytes() == csv(RENT)


def test_the_same_content_returns_the_same_import(
    tenant: Tenant, media_root: Path, enqueued_import: MagicMock
) -> None:
    """A second upload of the same bytes returns the first row and stores nothing else.

    Args:
        tenant: Active tenant.
        media_root: Temporary media directory.
        enqueued_import: Mock standing in for the task delay.
    """
    account = make_account()
    first = create_import(writer(tenant), account, csv(RENT))
    second = create_import(writer(tenant), account, csv(RENT))
    assert second.pk == first.pk
    assert BankStatementImport.objects.count() == 1
    assert len(list((media_root / f"tenants/{tenant.pk}/bank_imports").iterdir())) == 1


def test_different_content_is_a_new_import(tenant: Tenant) -> None:
    """Another file for the same account becomes its own import.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    first = create_import(writer(tenant), account, csv(RENT))
    second = create_import(writer(tenant), account, csv(RENT, COFFEE))
    assert second.pk != first.pk
    assert BankStatementImport.objects.count() == 2


def test_the_same_content_for_another_account_is_a_new_import(tenant: Tenant) -> None:
    """The idempotency key is per account, so a second account can upload the same bytes.

    Args:
        tenant: Active tenant.
    """
    other = BankAccount.objects.create(
        name="Wells",
        account_identifier="9999",
        date_column="Date",
        amount_column="Amount",
        description_column="Memo",
    )
    first = create_import(writer(tenant), make_account(), csv(RENT))
    second = create_import(writer(tenant), other, csv(RENT))
    assert second.pk != first.pk


class TestEnqueue(TestCase):
    """Creating an import enqueues the task after the transaction commits."""

    def test_create_import_enqueues_the_task_once_on_commit(self) -> None:
        """The task is queued once, only after commit, and a replay queues nothing."""
        tenant = Tenant.objects.create(name="A")
        delay = MagicMock()
        with tenant_context(tenant.pk):
            account = make_account()
            with (
                pytest.MonkeyPatch.context() as patch,
                self.captureOnCommitCallbacks(execute=True) as callbacks,
            ):
                patch.setattr(import_statement, "delay", delay)
                row = create_import(writer(tenant), account, csv(RENT))
                delay.assert_not_called()
            with (
                pytest.MonkeyPatch.context() as patch,
                self.captureOnCommitCallbacks(execute=True) as replay,
            ):
                patch.setattr(import_statement, "delay", delay)
                create_import(writer(tenant), account, csv(RENT))
        assert len(callbacks) == 1 and not replay
        delay.assert_called_once_with(str(tenant.pk), str(row.pk))

    def test_reuploading_after_a_failure_resets_the_import_and_enqueues_it_once(self) -> None:
        """The failed import is reused with the current mapping, cleared, and queued once."""
        tenant = Tenant.objects.create(name="A")
        delay = MagicMock()
        with tenant_context(tenant.pk):
            account = make_account()
            account.date_column = "Booked"
            account.save()
            row = create_import(writer(tenant), account, csv(RENT))
            import_statement(str(tenant.pk), str(row.pk))
            failed = BankStatementImport.objects.get(pk=row.pk)
            assert failed.status == "failed"
            BankStatementImport.objects.filter(pk=row.pk).update(
                rows_read=3, duplicates=1, imported=2
            )
            account.date_column = "Date"
            account.save()
            with (
                pytest.MonkeyPatch.context() as patch,
                self.captureOnCommitCallbacks(execute=True) as callbacks,
            ):
                patch.setattr(import_statement, "delay", delay)
                again = create_import(writer(tenant), account, csv(RENT))
            stored = BankStatementImport.objects.get(pk=row.pk)
            assert BankStatementImport.objects.count() == 1
        assert again.pk == row.pk and len(callbacks) == 1
        delay.assert_called_once_with(str(tenant.pk), str(row.pk))
        assert stored.mapping["date_column"] == "Date"
        assert (stored.status, stored.error, stored.completed_at) == ("pending", "", None)
        assert (stored.rows_read, stored.duplicates, stored.imported) == (0, 0, 0)


def run_import(tenant: Tenant, content: bytes) -> BankStatementImport:
    """Upload a file for the test account and run the task on it.

    Args:
        tenant: Active tenant.
        content: File content.

    Returns:
        The import as stored after the task.
    """
    account = BankAccount.objects.filter(name="Chase").first() or make_account()
    row = create_import(writer(tenant), account, content)
    import_statement(str(tenant.pk), str(row.pk))
    stored: BankStatementImport = BankStatementImport.objects.get(pk=row.pk)
    return stored


def test_the_task_imports_the_rows(tenant: Tenant) -> None:
    """Rows become transactions linked to the account and the import, with counters.

    Args:
        tenant: Active tenant.
    """
    row = run_import(tenant, csv(RENT, COFFEE))
    assert (row.status, row.rows_read, row.duplicates, row.imported) == ("completed", 2, 0, 2)
    assert row.completed_at is not None and row.error == ""
    rent, coffee = BankTransaction.objects.order_by("value_date")
    assert (rent.value_date, rent.amount, rent.description, rent.payer) == (
        date(2026, 1, 31),
        100000,
        "Rent",
        "Bob",
    )
    assert coffee.amount == -450
    assert {t.statement_import_id for t in (rent, coffee)} == {row.pk}
    assert {t.bank_account_id for t in (rent, coffee)} == {row.bank_account_id}
    assert {t.tenant_id for t in (rent, coffee)} == {tenant.pk}


def test_a_second_run_is_a_no_op(tenant: Tenant) -> None:
    """Running the task again on a finished import changes nothing.

    Args:
        tenant: Active tenant.
    """
    row = run_import(tenant, csv(RENT, COFFEE))
    import_statement(str(tenant.pk), str(row.pk))
    again = BankStatementImport.objects.get(pk=row.pk)
    assert (again.completed_at, again.imported, again.duplicates) == (row.completed_at, 2, 0)
    assert BankTransaction.objects.count() == 2


def test_an_overlapping_file_counts_duplicates_and_imports_the_rest(tenant: Tenant) -> None:
    """A second statement sharing rows with the first imports only the new ones.

    Args:
        tenant: Active tenant.
    """
    run_import(tenant, csv(RENT, COFFEE))
    second = run_import(tenant, csv(COFFEE, TAX))
    assert (second.rows_read, second.duplicates, second.imported) == (2, 1, 1)
    assert BankTransaction.objects.count() == 3
    assert BankTransaction.objects.get(description="Tax").statement_import_id == second.pk


def test_a_bad_file_fails_the_import_and_creates_nothing(tenant: Tenant) -> None:
    """A parse error marks the import failed with the message and stores no transaction.

    Args:
        tenant: Active tenant.
    """
    row = run_import(tenant, csv(RENT, "13/45/2026,1.00,Bad,\n"))
    assert row.status == "failed"
    assert "row 2" in row.error
    assert row.completed_at is not None
    assert (row.rows_read, row.duplicates, row.imported) == (0, 0, 0)
    assert not BankTransaction.objects.exists()


def test_a_fixed_mapping_imports_after_a_reset(tenant: Tenant) -> None:
    """After a failure and a re-upload, the task completes the same import with the new mapping.

    Args:
        tenant: Active tenant.
    """
    account = make_account()
    account.date_column = "Booked"
    account.save()
    failed = run_import(tenant, csv(RENT, COFFEE))
    assert failed.status == "failed"
    account.date_column = "Date"
    account.save()
    reset = create_import(writer(tenant), account, csv(RENT, COFFEE))
    import_statement(str(tenant.pk), str(reset.pk))
    done = BankStatementImport.objects.get(pk=failed.pk)
    assert (done.status, done.rows_read, done.imported, done.error) == ("completed", 2, 2, "")
    assert BankStatementImport.objects.count() == 1


def test_an_unknown_import_raises(tenant: Tenant) -> None:
    """An import the tenant cannot see is not found.

    Args:
        tenant: Active tenant.
    """
    with pytest.raises(BankStatementImport.DoesNotExist):
        import_statement(str(tenant.pk), "00000000-0000-0000-0000-000000000000")
