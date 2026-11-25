"""Tax rates: validation, permissions and the admin, all through the services."""

import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

import billing.admin
from accounts.models import User
from billing.models import TaxRate
from billing.services import delete_tax_rate, save_tax_rate
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db

MANAGER = frozenset(
    {Permission.INVOICES_READ, Permission.INVOICES_WRITE, Permission.SETTINGS_MANAGE}
)
ACCOUNTANT = frozenset({Permission.INVOICES_READ, Permission.INVOICES_WRITE})


def principal(tenant: Tenant, permissions: frozenset[Permission] = MANAGER) -> Principal:
    """Build a principal of a tenant.

    Args:
        tenant: Tenant the principal acts in.
        permissions: Permissions the principal holds, settings:manage included by default.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, permissions)


def test_save_tax_rate_persists_under_the_current_tenant() -> None:
    """A new tax rate is stored for the active tenant."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        saved = save_tax_rate(principal(tenant), TaxRate(name="Austin", rate_bp=825))
        assert TaxRate.objects.get(pk=saved.pk).rate_bp == 825
        assert saved.tenant_id == tenant.pk


@pytest.mark.parametrize("rate_bp", [0, 10000])
def test_save_tax_rate_accepts_the_range_limits(rate_bp: int) -> None:
    """Zero and one hundred percent are valid rates.

    Args:
        rate_bp: Rate in basis points.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        assert save_tax_rate(principal(tenant), TaxRate(name="Edge", rate_bp=rate_bp)).pk


@pytest.mark.parametrize("rate_bp", [10001, -1])
def test_save_tax_rate_rejects_rates_out_of_range(rate_bp: int) -> None:
    """A rate above 100 percent or below zero is refused and nothing is stored.

    Args:
        rate_bp: Rate in basis points.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        with pytest.raises(ValidationError):
            save_tax_rate(principal(tenant), TaxRate(name="Bad", rate_bp=rate_bp))
        assert TaxRate.objects.count() == 0


def test_save_tax_rate_rejects_a_duplicate_name_in_the_tenant_only() -> None:
    """A name is unique per tenant: reusing it raises ValidationError, another tenant may."""
    a, b = Tenant.objects.create(name="A"), Tenant.objects.create(name="B")
    with tenant_context(a.pk):
        save_tax_rate(principal(a), TaxRate(name="Austin", rate_bp=825))
        with pytest.raises(ValidationError):
            save_tax_rate(principal(a), TaxRate(name="Austin", rate_bp=100))
    with tenant_context(b.pk):
        assert save_tax_rate(principal(b), TaxRate(name="Austin", rate_bp=100)).pk


def test_save_tax_rate_updates_an_existing_row() -> None:
    """Saving a changed rate updates it, without tripping its own unique name."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        rate = save_tax_rate(principal(tenant), TaxRate(name="Austin", rate_bp=825))
        rate.rate_bp = 900
        save_tax_rate(principal(tenant), rate)
        assert list(TaxRate.objects.values_list("rate_bp", flat=True)) == [900]


def test_an_accountant_cannot_manage_tax_rates() -> None:
    """Without settings:manage, saving and deleting raise PermissionDenied."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        rate = save_tax_rate(principal(tenant), TaxRate(name="Austin", rate_bp=825))
        with pytest.raises(PermissionDenied):
            save_tax_rate(principal(tenant, ACCOUNTANT), TaxRate(name="Other", rate_bp=1))
        with pytest.raises(PermissionDenied):
            delete_tax_rate(principal(tenant, ACCOUNTANT), rate)
        assert TaxRate.objects.count() == 1


def test_delete_tax_rate_removes_the_row() -> None:
    """Deleting a tax rate removes it."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        rate = save_tax_rate(principal(tenant), TaxRate(name="Austin", rate_bp=825))
        delete_tax_rate(principal(tenant), rate)
        assert TaxRate.objects.count() == 0


@pytest.mark.django_db(transaction=True)
class TestTaxRateAdmin:
    """The tax rate admin writes through the services and is limited to settings:manage."""

    def login(self, client: Client, role: Membership.Role) -> Tenant:
        """Log in a user with a role in a new tenant and make it active.

        Args:
            client: Test client to prepare.
            role: Role of the membership.

        Returns:
            The tenant.
        """
        tenant = Tenant.objects.create(name="A")
        user = User.objects.create_superuser(f"u-{uuid.uuid4().hex[:6]}", "u@example.com", "pw")
        Membership.objects.create(user=user, tenant=tenant, role=role)
        client.force_login(user)
        session = client.session
        session["tenant_id"] = str(tenant.pk)
        session.save()
        return tenant

    def test_an_admin_adds_a_tax_rate_and_a_duplicate_shows_a_form_error(
        self, client: Client
    ) -> None:
        """Adding works, and reusing the name re-renders the form instead of failing.

        Args:
            client: Django test client fixture.
        """
        tenant = self.login(client, Membership.Role.ADMIN)
        url = "/admin/billing/taxrate/add/"
        assert client.post(url, {"name": "Austin", "rate_bp": "825"}).status_code == 302
        response = client.post(url, {"name": "Austin", "rate_bp": "100"})
        assert response.status_code == 200
        assert "already exists" in response.content.decode()
        with tenant_context(tenant.pk):
            assert list(TaxRate.objects.values_list("rate_bp", flat=True)) == [825]

    def test_an_accountant_cannot_open_the_add_form(self, client: Client) -> None:
        """Accountants lack settings:manage, so the add view answers 403.

        Args:
            client: Django test client fixture.
        """
        self.login(client, Membership.Role.ACCOUNTANT)
        assert client.get("/admin/billing/taxrate/add/").status_code == 403

    def test_delete_goes_through_the_service(self, client: Client) -> None:
        """Deleting from the admin removes the row.

        Args:
            client: Django test client fixture.
        """
        tenant = self.login(client, Membership.Role.OWNER)
        with tenant_context(tenant.pk):
            pk = TaxRate.objects.create(name="Austin", rate_bp=825).pk
        client.post(f"/admin/billing/taxrate/{pk}/delete/", {"post": "yes"})
        with tenant_context(tenant.pk):
            assert TaxRate.objects.count() == 0

    def test_bulk_delete_goes_through_the_service(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The delete-selected action calls delete_tax_rate for every row.

        Args:
            client: Django test client fixture.
            monkeypatch: Fixture used to record the service calls.
        """
        deleted: list[str] = []
        original = delete_tax_rate

        def record(principal: Principal | None, row: TaxRate) -> None:
            """Record the call, then delete through the real service.

            Args:
                principal: Acting principal.
                row: Row being deleted.
            """
            deleted.append(row.name)
            original(principal, row)

        monkeypatch.setattr(billing.admin, "delete_tax_rate", record)
        tenant = self.login(client, Membership.Role.OWNER)
        with tenant_context(tenant.pk):
            pks = [str(TaxRate.objects.create(name=name, rate_bp=100).pk) for name in ("a", "b")]
        data = {"action": "delete_selected", "_selected_action": pks, "post": "yes"}
        client.post("/admin/billing/taxrate/", data)
        assert sorted(deleted) == ["a", "b"]
        with tenant_context(tenant.pk):
            assert not TaxRate.objects.exists()
