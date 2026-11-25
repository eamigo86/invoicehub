"""The invoice read service: permission check and tenant scoping."""

import pytest
from django.core.exceptions import PermissionDenied

from billing.models import Customer, Invoice
from billing.services import list_invoices
from core.models import Currency
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db


def test_list_invoices_returns_the_invoices_of_the_tenant(
    tenant: Tenant, customer: Customer
) -> None:
    """A principal holding invoices:read gets the invoices of the active tenant.

    Args:
        tenant: Active tenant.
        customer: Customer of the active tenant.
    """
    invoice = Invoice.objects.create(customer=customer, currency=Currency.USD)
    reader = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    assert list(list_invoices(reader)) == [invoice]


def test_list_invoices_requires_invoices_read(tenant: Tenant) -> None:
    """A principal without invoices:read is refused.

    Args:
        tenant: Active tenant.
    """
    writer_only = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with pytest.raises(PermissionDenied):
        list_invoices(writer_only)
    with pytest.raises(PermissionDenied):
        list_invoices(None)
