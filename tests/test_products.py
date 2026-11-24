"""Products and their default taxes: validation, permissions, link replacement and the admin."""

import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from accounts.models import User
from billing.models import Product, ProductTaxRate, TaxRate
from billing.services import delete_product, save_product
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db

WRITER_PERMISSIONS = frozenset({Permission.INVOICES_READ, Permission.INVOICES_WRITE})


def writer(tenant: Tenant) -> Principal:
    """Build a principal that may write invoicing data, like an accountant.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, WRITER_PERMISSIONS)


def rates(*names: str) -> list[TaxRate]:
    """Create tax rates in the active tenant.

    Args:
        *names: Names of the tax rates to create.

    Returns:
        The saved tax rates in the order given.
    """
    return [TaxRate.objects.create(name=n, rate_bp=100) for n in names]


def linked(product: Product) -> list[str]:
    """List the names of a product's default taxes.

    Args:
        product: Product to inspect.

    Returns:
        The tax rate names, sorted.
    """
    return sorted(product.default_taxes.values_list("name", flat=True))


def test_save_product_persists_under_the_current_tenant() -> None:
    """A new product is stored in cents for the active tenant, without default taxes."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        saved = save_product(writer(tenant), Product(name="Widget", unit_price=1999))
        stored = Product.objects.get(pk=saved.pk)
        assert (stored.unit_price, stored.tenant_id, linked(stored)) == (1999, tenant.pk, [])


@pytest.mark.parametrize("price", [-1, 2**63])
def test_save_product_rejects_a_price_out_of_range(price: int) -> None:
    """A negative price, or one beyond a bigint, is refused and nothing is stored.

    Args:
        price: Unit price in cents.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        with pytest.raises(ValidationError):
            save_product(writer(tenant), Product(name="Widget", unit_price=price))
        assert Product.objects.count() == 0


def test_save_product_accepts_a_free_product() -> None:
    """A price of zero is valid."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        assert save_product(writer(tenant), Product(name="Free", unit_price=0)).pk


def test_save_product_sets_and_replaces_default_taxes() -> None:
    """Passing taxes links exactly those rates, dropping the earlier ones and repeats."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        city, state, county = rates("city", "state", "county")
        product = save_product(writer(tenant), Product(name="W", unit_price=1), [city, state, city])
        assert linked(product) == ["city", "state"]
        save_product(writer(tenant), product, [state, county])
        assert linked(product) == ["county", "state"]
        assert ProductTaxRate.objects.count() == 2


def test_save_product_without_taxes_keeps_the_links_and_an_empty_list_clears_them() -> None:
    """None leaves default taxes alone while an empty list removes them all."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        (city,) = rates("city")
        product = save_product(writer(tenant), Product(name="W", unit_price=1), [city])
        product.name = "W2"
        save_product(writer(tenant), product)
        assert linked(product) == ["city"]
        save_product(writer(tenant), product, [])
        assert linked(product) == []
        assert TaxRate.objects.count() == 1


def test_a_viewer_cannot_save_or_delete_products() -> None:
    """Without invoices:write, saving and deleting raise PermissionDenied."""
    tenant = Tenant.objects.create(name="A")
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with tenant_context(tenant.pk):
        product = save_product(writer(tenant), Product(name="W", unit_price=1))
        with pytest.raises(PermissionDenied):
            save_product(viewer, Product(name="X", unit_price=1))
        with pytest.raises(PermissionDenied):
            delete_product(viewer, product)
        assert Product.objects.count() == 1


def test_delete_product_removes_its_links_but_not_the_tax_rates() -> None:
    """Deleting a product cascades to its links and leaves the rates."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        (city,) = rates("city")
        product = save_product(writer(tenant), Product(name="W", unit_price=1), [city])
        delete_product(writer(tenant), product)
        assert (Product.objects.count(), ProductTaxRate.objects.count()) == (0, 0)
        assert TaxRate.objects.count() == 1


@pytest.mark.django_db(transaction=True)
class TestProductAdmin:
    """The product admin edits default taxes through a form field and the service."""

    def login(self, client: Client) -> Tenant:
        """Log in an accountant of a new tenant and make the tenant active.

        Args:
            client: Test client to prepare.

        Returns:
            The tenant.
        """
        tenant = Tenant.objects.create(name="A")
        user = User.objects.create_superuser(f"u-{uuid.uuid4().hex[:6]}", "u@example.com", "pw")
        Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.ACCOUNTANT)
        client.force_login(user)
        session = client.session
        session["tenant_id"] = str(tenant.pk)
        session.save()
        return tenant

    def test_add_and_change_set_the_default_taxes(self, client: Client) -> None:
        """Adding with taxes links them, and a change replaces them.

        Args:
            client: Django test client fixture.
        """
        tenant = self.login(client)
        with tenant_context(tenant.pk):
            city, state = rates("city", "state")
        data = {"name": "W", "description": "", "unit_price": "1500", "taxes": [str(city.pk)]}
        assert client.post("/admin/billing/product/add/", data).status_code == 302
        with tenant_context(tenant.pk):
            product = Product.objects.get()
            assert linked(product) == ["city"]
        data["taxes"] = [str(state.pk)]
        client.post(f"/admin/billing/product/{product.pk}/change/", data)
        with tenant_context(tenant.pk):
            assert linked(product) == ["state"]

    def test_the_change_form_preselects_the_current_taxes_of_the_tenant_only(
        self, client: Client
    ) -> None:
        """The taxes field is initialised from the links and offers no tax rate of another tenant.

        Args:
            client: Django test client fixture.
        """
        tenant = self.login(client)
        other = Tenant.objects.create(name="B")
        with tenant_context(other.pk):
            rates("foreign")
        with tenant_context(tenant.pk):
            city, _ = rates("city", "state")
            product = save_product(writer(tenant), Product(name="W", unit_price=1), [city])
        form = client.get(f"/admin/billing/product/{product.pk}/change/").context["adminform"].form
        assert form["taxes"].value() == [city.pk]
        with tenant_context(tenant.pk):
            assert sorted(str(t) for t in form.fields["taxes"].queryset) == ["city", "state"]

    def test_a_foreign_tax_rate_is_rejected_by_the_form(self, client: Client) -> None:
        """Posting another tenant's tax rate id re-renders the form and stores nothing.

        Args:
            client: Django test client fixture.
        """
        tenant = self.login(client)
        other = Tenant.objects.create(name="B")
        with tenant_context(other.pk):
            (foreign,) = rates("foreign")
        data = {"name": "W", "description": "", "unit_price": "1", "taxes": [str(foreign.pk)]}
        assert client.post("/admin/billing/product/add/", data).status_code == 200
        with tenant_context(tenant.pk):
            assert Product.objects.count() == 0


def test_the_unit_price_help_text_says_cents() -> None:
    """The unit price field tells admin users to enter cents."""
    assert "cents" in Product._meta.get_field("unit_price").help_text
