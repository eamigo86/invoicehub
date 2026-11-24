"""TenantModelAdmin combines Django permissions with the permission service."""

import pytest
from django.contrib import admin
from django.test import RequestFactory

from accounts.models import User
from billing.admin import CustomerAdmin
from billing.models import Customer
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal, set_request_principal

pytestmark = pytest.mark.django_db


class ReconciliationAdmin(CustomerAdmin):
    """Admin that requires the reconciliation permission to write."""

    write_permission = Permission.RECONCILIATION_WRITE


def test_defaults_are_invoice_permissions() -> None:
    """Read and write default to the invoice permissions."""
    assert CustomerAdmin.read_permission is Permission.INVOICES_READ
    assert CustomerAdmin.write_permission is Permission.INVOICES_WRITE


def test_required_permissions_are_overridable() -> None:
    """A subclass swaps the write permission, and the principal decides the outcome."""
    tenant = Tenant.objects.create(name="A")
    request = RequestFactory().get("/")
    request.user = User.objects.create_superuser("root", "root@example.com", "pw")
    model_admin = ReconciliationAdmin(Customer, admin.site)
    only_invoices = frozenset({Permission.INVOICES_READ, Permission.INVOICES_WRITE})
    set_request_principal(request, Principal(tenant.pk, only_invoices))
    assert not model_admin.has_change_permission(request)
    set_request_principal(
        request, Principal(tenant.pk, only_invoices | {Permission.RECONCILIATION_WRITE})
    )
    assert model_admin.has_change_permission(request)


def test_no_principal_means_no_access() -> None:
    """Without a principal even a superuser may neither view nor write."""
    request = RequestFactory().get("/")
    request.user = User.objects.create_superuser("root", "root@example.com", "pw")
    model_admin = CustomerAdmin(Customer, admin.site)
    assert not model_admin.has_view_permission(request)
    assert not model_admin.has_change_permission(request)
