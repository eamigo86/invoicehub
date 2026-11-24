"""Issuing invoices: gap-free numbering per series, frozen snapshots, dates and refusals."""

import threading
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from billing.models import AuditEvent, Customer, Invoice, InvoiceSeries
from billing.services import (
    DraftLine,
    InvalidTransition,
    delete_draft,
    issue_invoice,
    save_draft,
)
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db

FIXED_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def freeze_now(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Make django.utils.timezone.now return a fixed moment.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        moment: Moment to return.
    """
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant with complete issuer details and make it the active one.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        ein="12-3456789",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
        timezone="America/Los_Angeles",
    )
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
    """Build a principal that may write invoices, acting as the user.

    Args:
        tenant: Active tenant.
        user: User the principal stands for.

    Returns:
        The principal.
    """
    permissions = frozenset({Permission.INVOICES_READ, Permission.INVOICES_WRITE})
    return Principal(tenant.pk, permissions, user_id=user.pk)


@pytest.fixture
def customer(tenant: Tenant) -> Customer:
    """Create a customer in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The customer.
    """
    created: Customer = Customer.objects.create(
        name="Bolt", tax_id="98-7654321", emails=["ap@bolt.test"], city="Dallas"
    )
    return created


def draft(
    writer: Principal,
    customer: Customer,
    kind: Invoice.Kind = Invoice.Kind.INVOICE,
    due_date: date | None = None,
) -> Invoice:
    """Save a draft with one line.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        kind: Kind of document to draft.
        due_date: Due date of the draft, if any.

    Returns:
        The draft.
    """
    return save_draft(
        writer,
        Invoice(customer=customer, kind=kind, due_date=due_date),
        [DraftLine("Widget", Decimal("1"), 1000)],
    )


def test_numbers_and_references_are_consecutive(
    writer: Principal, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each issued invoice takes the next number and the INV- reference of the year.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        monkeypatch: Pytest monkeypatch fixture.
    """
    freeze_now(monkeypatch, FIXED_NOW)
    issued = [issue_invoice(writer, draft(writer, customer)) for _ in range(3)]
    assert [(i.number, i.year, i.reference) for i in issued] == [
        (1, 2026, "INV-2026-00001"),
        (2, 2026, "INV-2026-00002"),
        (3, 2026, "INV-2026-00003"),
    ]
    assert all(i.status == Invoice.Status.ISSUED for i in issued)
    assert InvoiceSeries.objects.get().next_number == 4


def test_each_kind_has_its_own_counter(
    writer: Principal, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credit note is numbered from its own CN- series, independent of the invoices.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        monkeypatch: Pytest monkeypatch fixture.
    """
    freeze_now(monkeypatch, FIXED_NOW)
    issue_invoice(writer, draft(writer, customer))
    note = issue_invoice(writer, draft(writer, customer, Invoice.Kind.CREDIT_NOTE))
    assert (note.number, note.reference) == (1, "CN-2026-00001")
    assert InvoiceSeries.objects.count() == 2


def test_the_counter_restarts_in_a_new_year(
    writer: Principal, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A series that last ran in an earlier year restarts at 1 and moves to the issue year.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        monkeypatch: Pytest monkeypatch fixture.
    """
    InvoiceSeries.objects.create(kind=Invoice.Kind.INVOICE, prefix="INV-", year=2025, next_number=7)
    freeze_now(monkeypatch, FIXED_NOW)
    issued = issue_invoice(writer, draft(writer, customer))
    assert (issued.number, issued.year) == (1, 2026)
    series = InvoiceSeries.objects.get()
    assert (series.year, series.next_number) == (2026, 2)


def test_the_reference_survives_a_prefix_change(
    writer: Principal, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the prefix affects later invoices only.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        monkeypatch: Pytest monkeypatch fixture.
    """
    freeze_now(monkeypatch, FIXED_NOW)
    first = issue_invoice(writer, draft(writer, customer))
    InvoiceSeries.objects.update(prefix="FAC-")
    second = issue_invoice(writer, draft(writer, customer))
    assert Invoice.objects.get(pk=first.pk).reference == "INV-2026-00001"
    assert second.reference == "FAC-2026-00002"


def test_issuer_and_customer_are_frozen_at_issue(
    writer: Principal, customer: Customer, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming the customer or the tenant afterwards does not change the invoice.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        tenant: Active tenant.
        monkeypatch: Pytest monkeypatch fixture.
    """
    freeze_now(monkeypatch, FIXED_NOW)
    issued = issue_invoice(writer, draft(writer, customer))
    Customer.objects.update(name="Renamed", city="Elsewhere")
    Tenant.objects.filter(pk=tenant.pk).update(legal_name="Renamed Inc.")
    stored = Invoice.objects.get(pk=issued.pk)
    assert stored.issuer is not None and stored.customer_details is not None
    assert stored.issuer["legal_name"] == "Acme Inc."
    assert stored.issuer["ein"] == "12-3456789"
    assert stored.issuer["currency"] == "USD"
    assert stored.customer_details["name"] == "Bolt"
    assert stored.customer_details["tax_id"] == "98-7654321"
    assert stored.customer_details["emails"] == ["ap@bolt.test"]
    assert stored.customer_details["city"] == "Dallas"
    assert stored.issued_at == FIXED_NOW


def test_the_issue_date_is_the_day_in_the_tenant_timezone(
    writer: Principal, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Just after midnight UTC on New Year, Los Angeles is still on the previous year.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        monkeypatch: Pytest monkeypatch fixture.
    """
    freeze_now(monkeypatch, datetime(2027, 1, 1, 7, 30, tzinfo=UTC))
    issued = issue_invoice(writer, draft(writer, customer))
    assert issued.issue_date == date(2026, 12, 31)
    assert (issued.year, issued.reference) == (2026, "INV-2026-00001")


def test_the_due_date_is_kept_unless_it_precedes_the_issue_date(
    writer: Principal, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft due date from the issue date on survives; otherwise it is 30 days after.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        monkeypatch: Pytest monkeypatch fixture.
    """
    freeze_now(monkeypatch, FIXED_NOW)
    today = date(2026, 6, 15)
    kept = issue_invoice(writer, draft(writer, customer, due_date=today + timedelta(days=5)))
    same_day = issue_invoice(writer, draft(writer, customer, due_date=today))
    past = issue_invoice(writer, draft(writer, customer, due_date=today - timedelta(days=1)))
    missing = issue_invoice(writer, draft(writer, customer))
    assert kept.due_date == today + timedelta(days=5)
    assert same_day.due_date == today
    assert past.due_date == today + timedelta(days=30)
    assert missing.due_date == today + timedelta(days=30)


def test_issuing_records_an_audit_event(writer: Principal, customer: Customer) -> None:
    """The issue is audited with the status before and after.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    issued = issue_invoice(writer, draft(writer, customer))
    event = AuditEvent.objects.get(action="invoice.issued")
    assert event.entity_id == issued.pk
    assert event.before is not None and event.after is not None
    assert (event.before["status"], event.after["status"]) == ("draft", "issued")
    assert event.after["reference"] == issued.reference


def test_only_a_draft_can_be_issued(writer: Principal, customer: Customer) -> None:
    """A second issue is refused and the series counter does not move.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    issued = issue_invoice(writer, draft(writer, customer))
    with pytest.raises(InvalidTransition):
        issue_invoice(writer, issued)
    assert InvoiceSeries.objects.get().next_number == 2


def test_an_invoice_without_lines_is_refused(writer: Principal, customer: Customer) -> None:
    """An empty draft cannot be issued and stays a draft.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    empty = save_draft(writer, Invoice(customer=customer), [])
    with pytest.raises(ValidationError, match="line"):
        issue_invoice(writer, empty)
    assert Invoice.objects.get().status == Invoice.Status.DRAFT
    assert not InvoiceSeries.objects.exists()


def test_an_incomplete_issuer_is_refused_naming_the_fields(
    writer: Principal, customer: Customer, tenant: Tenant
) -> None:
    """Missing legal name and city are named in the error and nothing is issued.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        tenant: Active tenant.
    """
    pending = draft(writer, customer)
    Tenant.objects.filter(pk=tenant.pk).update(legal_name="", city="")
    with pytest.raises(ValidationError) as caught:
        issue_invoice(writer, pending)
    message = " ".join(caught.value.messages)
    assert "legal_name" in message and "city" in message
    assert "postal_code" not in message
    assert Invoice.objects.get().status == Invoice.Status.DRAFT


def test_a_viewer_cannot_issue(writer: Principal, customer: Customer) -> None:
    """A principal without invoices:write is refused.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    pending = draft(writer, customer)
    viewer = Principal(writer.tenant_id, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        issue_invoice(viewer, pending)
    assert Invoice.objects.get().status == Invoice.Status.DRAFT


def test_editing_and_deleting_a_draft_lock_its_row(writer: Principal, customer: Customer) -> None:
    """Both draft services read the stored row with FOR UPDATE, serializing them with issuing.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    pending = draft(writer, customer)
    with CaptureQueriesContext(connection) as saving:
        save_draft(writer, pending, [DraftLine("Other", Decimal("1"), 1)])
    with CaptureQueriesContext(connection) as deleting:
        delete_draft(writer, pending)
    for captured in (saving, deleting):
        locks = [q["sql"] for q in captured.captured_queries if "FOR UPDATE" in q["sql"]]
        assert any('FROM "billing_invoice"' in sql for sql in locks)


@pytest.mark.django_db(transaction=True)
def test_concurrent_issues_take_every_number_exactly_once() -> None:
    """Ten threads issuing different drafts of one tenant get the numbers 1 to 10, no gaps.

    The tenant fixtures are not used, because they keep an open transaction that the threads
    could not see; the setup commits its own tenant contexts instead.
    """
    tenant = Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    writer = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt")
        drafts = [draft(writer, customer) for _ in range(10)]
    barrier = threading.Barrier(len(drafts))
    errors: list[BaseException] = []

    def work(pending: Invoice) -> None:
        """Issue one draft in its own connection and tenant context.

        Args:
            pending: Draft to issue.
        """
        try:
            with tenant_context(tenant.pk):
                barrier.wait(timeout=10)
                issue_invoice(writer, pending)
        except BaseException as error:  # noqa: BLE001 - collected and asserted below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=work, args=(pending,)) for pending in drafts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    with tenant_context(tenant.pk):
        assert sorted(Invoice.objects.values_list("number", flat=True)) == list(range(1, 11))
        assert InvoiceSeries.objects.get().next_number == 11
