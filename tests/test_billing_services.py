"""Customer services, the single write path for views, the admin and later the API."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from billing.models import Customer
from billing.services import delete_customer, save_customer, update_issuer
from tenancy.context import tenant_context
from tenancy.models import Tenant
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


def viewer(tenant: Tenant) -> Principal:
    """Build a principal that may only read, like a viewer.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))


def test_save_customer_persists_under_the_current_tenant() -> None:
    """A new customer is stored for the active tenant and returned."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        saved = save_customer(writer(tenant), Customer(name="Acme"))
        assert saved.pk is not None
        assert Customer.objects.get(pk=saved.pk).tenant_id == tenant.pk


def test_save_customer_updates_an_existing_row() -> None:
    """Saving a changed customer updates it instead of creating another."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        customer = save_customer(writer(tenant), Customer(name="Acme"))
        customer.name = "Acme Ltd"
        save_customer(writer(tenant), customer)
        assert list(Customer.objects.values_list("name", flat=True)) == ["Acme Ltd"]


def test_delete_customer_removes_the_row() -> None:
    """Deleting a customer removes it from the tenant's data."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        customer = save_customer(writer(tenant), Customer(name="Acme"))
        delete_customer(writer(tenant), customer)
        assert Customer.objects.count() == 0


def test_a_viewer_cannot_save_a_customer() -> None:
    """Without invoices:write the save is refused and nothing is stored."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        with pytest.raises(PermissionDenied):
            save_customer(viewer(tenant), Customer(name="Acme"))
        assert Customer.objects.count() == 0


def test_a_viewer_cannot_delete_a_customer() -> None:
    """Without invoices:write the delete is refused and the row stays."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        customer = save_customer(writer(tenant), Customer(name="Acme"))
        with pytest.raises(PermissionDenied):
            delete_customer(viewer(tenant), customer)
        assert Customer.objects.count() == 1


def test_no_principal_is_refused() -> None:
    """A missing principal is refused like a principal without the permission."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk), pytest.raises(PermissionDenied):
        save_customer(None, Customer(name="Acme"))


def manager(tenant: Tenant) -> Principal:
    """Build a principal that may manage settings, like an owner.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, frozenset(Permission))


def test_update_issuer_saves_the_issuer_fields() -> None:
    """An owner updates legal name, EIN, address, currency and timezone."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        tenant.legal_name = "A LLC"
        tenant.ein = "12-3456789"
        tenant.city = "Austin"
        tenant.default_currency = "CAD"
        tenant.timezone = "America/Toronto"
        update_issuer(manager(tenant), tenant)
    stored = Tenant.objects.get(pk=tenant.pk)
    assert (stored.legal_name, stored.ein, stored.city) == ("A LLC", "12-3456789", "Austin")
    assert (stored.default_currency, stored.timezone) == ("CAD", "America/Toronto")


def test_update_issuer_cannot_change_the_tenant_name() -> None:
    """Only issuer columns are written, so a changed name is not persisted."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        tenant.name = "Renamed"
        tenant.legal_name = "A LLC"
        update_issuer(manager(tenant), tenant)
    stored = Tenant.objects.get(pk=tenant.pk)
    assert (stored.name, stored.legal_name) == ("A", "A LLC")


def test_update_issuer_rejects_invalid_data() -> None:
    """A malformed EIN raises ValidationError and nothing is stored."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        tenant.ein = "nope"
        with pytest.raises(ValidationError):
            update_issuer(manager(tenant), tenant)
    assert Tenant.objects.get(pk=tenant.pk).ein == ""


def test_update_issuer_needs_settings_manage() -> None:
    """An accountant holding invoices:write is refused."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        tenant.legal_name = "A LLC"
        with pytest.raises(PermissionDenied):
            update_issuer(writer(tenant), tenant)
    assert Tenant.objects.get(pk=tenant.pk).legal_name == ""


def test_save_customer_rejects_an_invalid_email() -> None:
    """Every address in emails is validated before saving."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        with pytest.raises(ValidationError):
            save_customer(writer(tenant), Customer(name="Acme", emails=["ok@example.com", "bad"]))
        assert Customer.objects.count() == 0


def test_save_customer_stores_emails_and_address() -> None:
    """A customer keeps several emails and a postal address."""
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        saved = save_customer(
            writer(tenant),
            Customer(name="Acme", emails=["a@example.com", "b@example.com"], city="Denver"),
        )
        stored = Customer.objects.get(pk=saved.pk)
        assert stored.emails == ["a@example.com", "b@example.com"]
        assert (stored.city, stored.country) == ("Denver", "US")
