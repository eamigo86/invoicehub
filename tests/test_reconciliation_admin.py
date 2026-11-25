"""Reconciliation admins: bank accounts, the statement upload, imports and transactions.

These tests use real transactions because the tenant reaches PostgreSQL only when a request's
outermost transaction begins.
"""

import datetime
from unittest.mock import MagicMock

import pytest
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from accounts.models import User
from reconciliation import services
from reconciliation.models import BankAccount, BankStatementImport, BankTransaction
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db(transaction=True)

BASE = "/admin/reconciliation"
CSV = b"Date,Amount,Memo\n01/31/2026,1000.00,Rent\n"


def login(client: Client, tenant: Tenant, role: Membership.Role = Membership.Role.OWNER) -> None:
    """Log in a member of the tenant and store it as the active tenant in the session.

    Args:
        client: Test client to prepare.
        tenant: Tenant to activate.
        role: Role of the logged in member.
    """
    user = User.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(user)
    Membership.objects.create(user=user, tenant=tenant, role=role)
    session = client.session
    session["tenant_id"] = str(tenant.pk)
    session.save()


@pytest.fixture
def tenant() -> Tenant:
    """Create a tenant with one bank account, one import and one transaction.

    Returns:
        The tenant.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        account = BankAccount.objects.create(
            name="Chase",
            account_identifier="1234",
            date_column="Date",
            amount_column="Amount",
            description_column="Memo",
        )
        statement = BankStatementImport.objects.create(
            bank_account=account, file_sha256="a" * 64, mapping={"date_column": "Date"}
        )
        BankTransaction.objects.create(
            bank_account=account,
            statement_import=statement,
            value_date=datetime.date(2026, 6, 15),
            amount=-1250,
            description="Coffee",
            dedupe_hash="b" * 64,
        )
    return tenant


def writer(tenant: Tenant) -> Principal:
    """Build a principal who may write reconciliation data.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, frozenset({Permission.RECONCILIATION_WRITE}))


def test_save_bank_account_needs_reconciliation_write(tenant: Tenant) -> None:
    """A principal without reconciliation:write is refused and nothing is stored.

    Args:
        tenant: Active tenant.
    """
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    account = BankAccount(name="Wells", account_identifier="9", date_column="D", amount_column="A")
    with tenant_context(tenant.pk):
        with pytest.raises(PermissionDenied):
            services.save_bank_account(viewer, account)
        assert not BankAccount.objects.filter(name="Wells").exists()


def test_save_bank_account_refuses_a_duplicate_name_within_the_tenant(tenant: Tenant) -> None:
    """A second account with the same name is refused in the tenant but fine in another one.

    Args:
        tenant: Active tenant, which already has the account Chase.
    """
    other = Tenant.objects.create(name="B")

    def chase() -> BankAccount:
        """Build an unsaved account named Chase.

        Returns:
            The account.
        """
        return BankAccount(
            name="Chase",
            account_identifier="9",
            date_column="D",
            amount_column="A",
            description_column="M",
        )

    with tenant_context(tenant.pk), pytest.raises(ValidationError):
        services.save_bank_account(writer(tenant), chase())
    with tenant_context(other.pk):
        saved = services.save_bank_account(Principal(other.pk, writer(tenant).permissions), chase())
        assert saved.tenant_id == other.pk


@pytest.mark.parametrize("model", ["bankaccount", "bankstatementimport", "banktransaction"])
def test_changelists_render_for_an_owner(client: Client, tenant: Tenant, model: str) -> None:
    """Each reconciliation changelist answers 200 for an owner.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the rows.
        model: Model name in the admin URL.
    """
    login(client, tenant)
    assert client.get(f"{BASE}/{model}/").status_code == 200


def test_adding_an_account_goes_through_the_service_and_reports_duplicates(
    client: Client, tenant: Tenant
) -> None:
    """The add form saves an account, and a taken name is a form error instead of a crash.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the account Chase.
    """
    login(client, tenant)
    data = {
        "name": "Wells",
        "account_identifier": "9",
        "currency": "USD",
        "date_column": "Date",
        "amount_column": "Amount",
        "description_column": "Memo",
        "payer_column": "",
        "date_format": "%m/%d/%Y",
    }
    assert client.post(f"{BASE}/bankaccount/add/", data).status_code == 302
    again = client.post(f"{BASE}/bankaccount/add/", data)
    assert again.status_code == 200
    assert "already exists" in again.content.decode()
    with tenant_context(tenant.pk):
        assert BankAccount.objects.filter(name="Wells").count() == 1


def test_uploading_a_csv_creates_one_import_and_enqueues_once(
    client: Client, tenant: Tenant, enqueued_import: MagicMock
) -> None:
    """The upload stores the file as a pending import and lands on its page.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the account Chase.
        enqueued_import: Mock standing in for the import task delay.
    """
    login(client, tenant)
    with tenant_context(tenant.pk):
        account = BankAccount.objects.get(name="Chase")
    upload = SimpleUploadedFile("s.csv", CSV, "text/csv")
    response = client.post(
        f"{BASE}/bankstatementimport/add/", {"bank_account": str(account.pk), "file": upload}
    )
    with tenant_context(tenant.pk):
        created = BankStatementImport.objects.exclude(file_sha256="a" * 64).get()
    assert response.status_code == 302
    assert response["Location"] == f"{BASE}/bankstatementimport/{created.pk}/change/"
    assert created.status == BankStatementImport.Status.PENDING
    enqueued_import.assert_called_once_with(str(tenant.pk), str(created.pk))


def test_uploading_the_same_file_again_lands_on_the_same_import(
    client: Client, tenant: Tenant, enqueued_import: MagicMock
) -> None:
    """A repeated upload redirects to the first import and enqueues nothing more.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the account Chase.
        enqueued_import: Mock standing in for the import task delay.
    """
    login(client, tenant)
    with tenant_context(tenant.pk):
        account = BankAccount.objects.get(name="Chase")
    locations = []
    for _ in range(2):
        upload = SimpleUploadedFile("s.csv", CSV, "text/csv")
        post = {"bank_account": str(account.pk), "file": upload}
        locations.append(client.post(f"{BASE}/bankstatementimport/add/", post)["Location"])
    assert locations[0] == locations[1]
    with tenant_context(tenant.pk):
        assert BankStatementImport.objects.count() == 2  # the fixture one and the upload
    assert enqueued_import.call_count == 1


def test_an_oversized_upload_is_a_form_error_and_stores_nothing(
    client: Client,
    tenant: Tenant,
    enqueued_import: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file over the size limit re-renders the form with an error instead of crashing.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the account Chase.
        enqueued_import: Mock standing in for the import task delay.
        monkeypatch: Pytest monkeypatch fixture, used to shrink the limit.
    """
    monkeypatch.setattr("reconciliation.services.MAX_STATEMENT_BYTES", len(CSV) - 1)
    login(client, tenant)
    with tenant_context(tenant.pk):
        account = BankAccount.objects.get(name="Chase")
    upload = SimpleUploadedFile("s.csv", CSV, "text/csv")
    response = client.post(
        f"{BASE}/bankstatementimport/add/", {"bank_account": str(account.pk), "file": upload}
    )
    assert response.status_code == 200
    assert "too large" in response.content.decode()
    with tenant_context(tenant.pk):
        assert BankStatementImport.objects.count() == 1
    enqueued_import.assert_not_called()


def test_a_viewer_cannot_upload(client: Client, tenant: Tenant) -> None:
    """A viewer gets 403 on the upload form and the post stores nothing.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the account Chase.
    """
    login(client, tenant, Membership.Role.VIEWER)
    with tenant_context(tenant.pk):
        account = BankAccount.objects.get(name="Chase")
    url = f"{BASE}/bankstatementimport/add/"
    assert client.get(url).status_code == 403
    upload = SimpleUploadedFile("s.csv", CSV, "text/csv")
    assert client.post(url, {"bank_account": str(account.pk), "file": upload}).status_code == 403
    with tenant_context(tenant.pk):
        assert BankStatementImport.objects.count() == 1


def test_the_import_page_is_a_read_only_detail(client: Client, tenant: Tenant) -> None:
    """The import page shows the outcome and links its transactions, and cannot be changed.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the import.
    """
    login(client, tenant)
    with tenant_context(tenant.pk):
        statement = BankStatementImport.objects.get()
    url = f"{BASE}/bankstatementimport/{statement.pk}/change/"
    body = client.get(url).content.decode()
    assert "Pending" in body
    assert f"/banktransaction/?statement_import__id__exact={statement.pk}" in body
    assert client.post(url, {}).status_code == 403
    assert client.post(f"{url.removesuffix('change/')}delete/", {"post": "yes"}).status_code == 403
    with tenant_context(tenant.pk):
        assert BankStatementImport.objects.filter(pk=statement.pk).exists()


def test_the_transaction_page_is_read_only(client: Client, tenant: Tenant) -> None:
    """A transaction page renders its signed amount and refuses any change.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the transaction.
    """
    login(client, tenant)
    with tenant_context(tenant.pk):
        row = BankTransaction.objects.get()
    url = f"{BASE}/banktransaction/{row.pk}/change/"
    assert client.get(url).status_code == 200
    assert "-12.50 USD" in client.get(f"{BASE}/banktransaction/").content.decode()
    assert client.post(url, {"description": "x"}).status_code == 403


def test_a_bank_account_cannot_be_deleted(client: Client, tenant: Tenant) -> None:
    """Imports reference accounts with PROTECT, so the admin offers no delete.

    Args:
        client: Django test client fixture.
        tenant: Tenant owning the account.
    """
    login(client, tenant)
    with tenant_context(tenant.pk):
        account = BankAccount.objects.get()
    assert (
        client.post(f"{BASE}/bankaccount/{account.pk}/delete/", {"post": "yes"}).status_code == 403
    )
    with tenant_context(tenant.pk):
        assert BankAccount.objects.filter(pk=account.pk).exists()


@pytest.mark.parametrize(
    ("cents", "shown"),
    [
        (1250, "12.50 USD"),
        (-1250, "-12.50 USD"),
        (-5, "-0.05 USD"),
        (0, "0.00 USD"),
        (123456789, "1,234,567.89 USD"),
    ],
)
def test_the_amount_is_shown_with_its_sign_using_integer_math(cents: int, shown: str) -> None:
    """Cents become a signed amount with the account currency, without floats.

    Args:
        cents: Stored amount.
        shown: Expected display text.
    """
    from reconciliation.admin import BankTransactionAdmin

    row = BankTransaction(amount=cents, bank_account=BankAccount(currency="USD"))
    assert BankTransactionAdmin(BankTransaction, admin.site).amount_display(row) == shown
