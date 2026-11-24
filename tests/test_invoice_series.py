"""Invoice series: the service that edits the prefix only, its admin and the issue action."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from accounts.models import User
from billing.models import Customer, Invoice, InvoiceSeries
from billing.services import DraftLine, save_draft, save_series
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
