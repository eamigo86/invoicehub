"""Invoice series: the service that edits the prefix only, its admin and the issue action."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import ProtectedError
from django.test import Client

import billing.admin
from accounts.models import User
from billing.models import Customer, Invoice, InvoiceSeries
from billing.services import DraftLine, delete_series, issue_invoice, save_draft, save_series
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal

ADD_URL = "/admin/billing/invoiceseries/add/"
INVOICES_URL = "/admin/billing/invoice/"


@pytest.fixture
def tenant() -> Tenant:
    """Create a tenant in Los Angeles with complete issuer details.

    Returns:
        The tenant.
    """
    return Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
        timezone="America/Los_Angeles",
    )


@pytest.mark.django_db
def test_a_new_series_starts_at_one_in_the_tenant_year(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The year is the current one in the tenant timezone, not in UTC.

    Args:
        tenant: Tenant whose timezone is Los Angeles.
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2027, 1, 1, 7, 30, tzinfo=UTC)
    )
    principal = Principal(tenant.pk, frozenset({Permission.SETTINGS_MANAGE}))
    with tenant_context(tenant.pk):
        series = save_series(
            principal,
            InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-", year=1, next_number=9),
        )
        stored = InvoiceSeries.objects.get()
    assert (stored.pk, stored.year, stored.next_number) == (series.pk, 2026, 1)
    assert str(stored) == "INV- (invoice)"


@pytest.mark.django_db
def test_an_existing_series_only_changes_its_prefix(tenant: Tenant) -> None:
    """Counter, year and kind written on the instance never reach the database.

    Args:
        tenant: Tenant that owns the series.
    """
    principal = Principal(tenant.pk, frozenset({Permission.SETTINGS_MANAGE}))
    with tenant_context(tenant.pk):
        series = InvoiceSeries.objects.create(
            kind=Invoice.Kind.INVOICE, prefix="INV-", year=2026, next_number=5
        )
        series.prefix, series.next_number, series.year = "FAC-", 99, 1999
        series.kind = Invoice.Kind.CREDIT_NOTE
        saved = save_series(principal, series)
        stored = InvoiceSeries.objects.get()
    assert (stored.prefix, stored.next_number, stored.year, stored.kind) == (
        "FAC-",
        5,
        2026,
        Invoice.Kind.INVOICE,
    )
    assert (saved.next_number, saved.year, saved.kind) == (5, 2026, Invoice.Kind.INVOICE)


@pytest.mark.django_db
def test_a_second_series_of_a_kind_is_refused(tenant: Tenant) -> None:
    """A tenant has one series per kind.

    Args:
        tenant: Tenant that owns the series.
    """
    principal = Principal(tenant.pk, frozenset({Permission.SETTINGS_MANAGE}))
    with tenant_context(tenant.pk):
        save_series(principal, InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
        with pytest.raises(ValidationError):
            save_series(principal, InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="FAC-"))
        assert InvoiceSeries.objects.count() == 1


@pytest.mark.django_db
def test_saving_a_series_needs_settings_manage(tenant: Tenant) -> None:
    """An accountant, who lacks settings:manage, is refused and nothing is stored.

    Args:
        tenant: Tenant that owns the series.
    """
    accountant = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        with pytest.raises(PermissionDenied):
            save_series(accountant, InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
        assert not InvoiceSeries.objects.exists()


def login(client: Client, tenant: Tenant, role: Membership.Role) -> None:
    """Log in a superuser holding a role in the tenant and make the tenant active.

    Args:
        client: Test client to prepare.
        tenant: Tenant to activate.
        role: Role of the membership.
    """
    user = User.objects.create_superuser("root", "root@example.com", "pw")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    client.force_login(user)
    session = client.session
    session["tenant_id"] = str(tenant.pk)
    session.save()


@pytest.mark.django_db(transaction=True)
def test_the_series_admin_adds_and_edits_through_the_service(
    tenant: Tenant, client: Client
) -> None:
    """Adding stores the current year at 1; editing changes the prefix and ignores the counter.

    Args:
        tenant: Tenant that owns the series.
        client: Test client.
    """
    login(client, tenant, Membership.Role.ADMIN)
    assert client.post(ADD_URL, {"kind": "invoice", "prefix": "INV-"}).status_code == 302
    with tenant_context(tenant.pk):
        series = InvoiceSeries.objects.get()
        InvoiceSeries.objects.update(next_number=7)
    assert series.next_number == 1
    change = f"/admin/billing/invoiceseries/{series.pk}/change/"
    form = client.get(change).context["adminform"].form
    assert list(form.fields) == ["prefix"]
    data = {"prefix": "FAC-", "next_number": "99", "year": "1999", "kind": "credit_note"}
    assert client.post(change, data).status_code == 302
    with tenant_context(tenant.pk):
        stored = InvoiceSeries.objects.get()
    assert (stored.prefix, stored.next_number, stored.kind) == ("FAC-", 7, "invoice")
    assert stored.year == series.year


@pytest.mark.django_db(transaction=True)
def test_adding_a_duplicate_kind_in_the_admin_shows_a_form_error(
    tenant: Tenant, client: Client
) -> None:
    """The second series of a kind is reported as a field error, not as a server error.

    Args:
        tenant: Tenant that owns the series.
        client: Test client.
    """
    login(client, tenant, Membership.Role.ADMIN)
    client.post(ADD_URL, {"kind": "invoice", "prefix": "INV-"})
    response = client.post(ADD_URL, {"kind": "invoice", "prefix": "FAC-"})
    assert response.status_code == 200
    assert "already exists" in response.content.decode()
    with tenant_context(tenant.pk):
        assert InvoiceSeries.objects.count() == 1


def make_drafts(tenant: Tenant) -> tuple[Invoice, Invoice]:
    """Create one draft with a line and one without, in committed contexts.

    Args:
        tenant: Tenant that owns the drafts.

    Returns:
        The draft with a line and the empty draft.
    """
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt")
        good = save_draft(principal, Invoice(customer=customer), [DraftLine("A", Decimal(1), 5)])
        empty = save_draft(principal, Invoice(customer=customer), [])
    return good, empty


@pytest.mark.django_db(transaction=True)
def test_the_issue_action_reports_each_success_and_failure(tenant: Tenant, client: Client) -> None:
    """Selecting a good and an empty draft issues the first and explains the second.

    Args:
        tenant: Tenant that owns the drafts.
        client: Test client.
    """
    login(client, tenant, Membership.Role.ACCOUNTANT)
    good, empty = make_drafts(tenant)
    assert "Issue selected drafts" in client.get(INVOICES_URL).content.decode()
    response = client.post(
        INVOICES_URL,
        {"action": "issue_selected_drafts", "_selected_action": [str(good.pk), str(empty.pk)]},
        follow=True,
    )
    messages = [str(m) for m in response.context["messages"]]
    assert any("INV-" in m and "00001" in m for m in messages)
    assert any("at least one line" in m for m in messages)
    with tenant_context(tenant.pk):
        statuses = {i.pk: i.status for i in Invoice.objects.all()}
    assert statuses == {good.pk: "issued", empty.pk: "draft"}


@pytest.mark.django_db(transaction=True)
def test_a_viewer_has_no_issue_action(tenant: Tenant, client: Client) -> None:
    """Without invoices:write the action is not offered and nothing is issued.

    Args:
        tenant: Tenant that owns the drafts.
        client: Test client.
    """
    login(client, tenant, Membership.Role.VIEWER)
    good, _ = make_drafts(tenant)
    assert "Issue selected drafts" not in client.get(INVOICES_URL).content.decode()
    client.post(
        INVOICES_URL, {"action": "issue_selected_drafts", "_selected_action": [str(good.pk)]}
    )
    with tenant_context(tenant.pk):
        assert Invoice.objects.get(pk=good.pk).status == "draft"


@pytest.mark.django_db
def test_delete_series_removes_an_unused_series_and_needs_settings_manage(tenant: Tenant) -> None:
    """A settings:manage holder deletes a series nobody used; an accountant is refused.

    Args:
        tenant: Tenant that owns the series.
    """
    manager = Principal(tenant.pk, frozenset({Permission.SETTINGS_MANAGE}))
    accountant = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        series = save_series(manager, InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
        with pytest.raises(PermissionDenied):
            delete_series(accountant, series)
        assert InvoiceSeries.objects.exists()
        delete_series(manager, series)
        assert not InvoiceSeries.objects.exists()


@pytest.mark.django_db
def test_a_series_with_invoices_cannot_be_deleted(tenant: Tenant) -> None:
    """The protected foreign key refuses to delete a series that numbered an invoice.

    Args:
        tenant: Tenant that owns the series.
    """
    principal = Principal(tenant.pk, frozenset(Permission))
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt")
        pending = save_draft(principal, Invoice(customer=customer), [DraftLine("A", Decimal(1), 5)])
        issue_invoice(principal, pending)
        with pytest.raises(ProtectedError):
            delete_series(principal, InvoiceSeries.objects.get())
        assert InvoiceSeries.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_the_series_admin_deletes_through_the_service(
    tenant: Tenant, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete page and the bulk action both call delete_series.

    Args:
        tenant: Tenant that owns the series.
        client: Test client.
        monkeypatch: Pytest monkeypatch fixture.
    """
    login(client, tenant, Membership.Role.ADMIN)
    principal = Principal(tenant.pk, frozenset(Permission))
    with tenant_context(tenant.pk):
        first = save_series(principal, InvoiceSeries(kind=Invoice.Kind.INVOICE, prefix="INV-"))
        second = save_series(principal, InvoiceSeries(kind=Invoice.Kind.CREDIT_NOTE, prefix="CN-"))
    deleted: list[str] = []
    original = delete_series

    def record(who: Principal | None, row: InvoiceSeries) -> None:
        """Record the call, then delete through the real service.

        Args:
            who: Acting principal.
            row: Series being deleted.
        """
        deleted.append(row.prefix)
        original(who, row)

    monkeypatch.setattr(billing.admin, "delete_series", record)
    url = "/admin/billing/invoiceseries/"
    assert client.post(f"{url}{first.pk}/delete/", {"post": "yes"}).status_code == 302
    data: dict[str, Any] = {
        "action": "delete_selected",
        "_selected_action": [str(second.pk)],
        "post": "yes",
    }
    assert client.post(url, data).status_code == 302
    assert deleted == ["INV-", "CN-"]
    with tenant_context(tenant.pk):
        assert not InvoiceSeries.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_the_issue_action_takes_documents_in_a_fixed_order(
    tenant: Tenant, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drafts are issued ordered by kind and then pk, whatever the order they were selected in.

    Args:
        tenant: Tenant that owns the drafts.
        client: Test client.
        monkeypatch: Pytest monkeypatch fixture.
    """
    login(client, tenant, Membership.Role.ACCOUNTANT)
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt")
        lines = [DraftLine("A", Decimal(1), 5)]
        drafts = [
            save_draft(principal, Invoice(customer=customer, kind=kind), lines)
            for kind in ("invoice", "credit_note", "invoice", "credit_note")
        ]
    seen: list[Any] = []
    original = issue_invoice

    def record(who: Principal | None, row: Invoice) -> Invoice:
        """Record the invoice, then issue it through the real service.

        Args:
            who: Acting principal.
            row: Draft being issued.

        Returns:
            The issued invoice.
        """
        seen.append(row.pk)
        return original(who, row)

    monkeypatch.setattr(billing.admin, "issue_invoice", record)
    client.post(
        INVOICES_URL,
        {
            "action": "issue_selected_drafts",
            "_selected_action": [str(d.pk) for d in reversed(drafts)],
        },
    )
    notes = sorted(drafts[i].pk for i in (1, 3))
    invoices = sorted(drafts[i].pk for i in (0, 2))
    assert seen == notes + invoices


@pytest.mark.django_db(transaction=True)
def test_the_issue_action_keeps_going_after_failures(
    tenant: Tenant, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid and vanished drafts are reported, and the others still get consecutive numbers.

    Args:
        tenant: Tenant that owns the drafts.
        client: Test client.
        monkeypatch: Pytest monkeypatch fixture.
    """
    login(client, tenant, Membership.Role.ACCOUNTANT)
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt")
        lines = [DraftLine("A", Decimal(1), 5)]
        one = save_draft(principal, Invoice(customer=customer), lines)
        empty = save_draft(principal, Invoice(customer=customer), [])
        vanishing = save_draft(principal, Invoice(customer=customer), lines)
        two = save_draft(principal, Invoice(customer=customer), lines)
    original = issue_invoice

    def delete_before(who: Principal | None, row: Invoice) -> Invoice:
        """Delete the vanishing draft just before it would be issued, as another request could.

        Args:
            who: Acting principal.
            row: Draft being issued.

        Returns:
            The issued invoice.
        """
        if row.pk == vanishing.pk:
            Invoice.objects.filter(pk=vanishing.pk).delete()
        return original(who, row)

    monkeypatch.setattr(billing.admin, "issue_invoice", delete_before)
    selected = [str(d.pk) for d in (one, empty, vanishing, two)]
    response = client.post(
        INVOICES_URL,
        {"action": "issue_selected_drafts", "_selected_action": selected},
        follow=True,
    )
    messages = [str(m) for m in response.context["messages"]]
    assert sum("Issued INV-" in m for m in messages) == 2
    assert any("at least one line" in m for m in messages)
    assert any(str(vanishing.pk) in m for m in messages)
    with tenant_context(tenant.pk):
        numbers = Invoice.objects.exclude(number=None).order_by("number")
        assert [i.number for i in numbers] == [1, 2]
