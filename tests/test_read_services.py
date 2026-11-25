"""The read services for customers, products, payments, runs and proposals."""

from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied

from billing.models import Customer, Product, TaxRate
from billing.services import list_customers, list_payments, list_products, list_tax_rates
from reconciliation.services import list_match_proposals, list_reconciliation_runs
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice, pay
from tests.test_reconciliation_apply import propose

pytestmark = pytest.mark.django_db

READ_SERVICES: list[Callable[[Principal | None], Any]] = [
    list_customers,
    list_products,
    list_payments,
    list_tax_rates,
    list_reconciliation_runs,
    list_match_proposals,
]


@pytest.mark.parametrize("service", READ_SERVICES, ids=lambda service: service.__name__)
def test_a_read_service_requires_invoices_read(
    tenant: Tenant, service: Callable[[Principal | None], Any]
) -> None:
    """A principal without invoices:read, or none at all, is refused.

    Args:
        tenant: Active tenant.
        service: Read service under test.
    """
    writer_only = Principal(
        tenant.pk, frozenset({Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE})
    )
    with pytest.raises(PermissionDenied):
        service(writer_only)
    with pytest.raises(PermissionDenied):
        service(None)


def test_the_read_services_return_the_rows_of_the_tenant(
    tenant: Tenant, customer: Customer
) -> None:
    """A principal holding invoices:read gets every row of each kind.

    Args:
        tenant: Active tenant.
        customer: Customer of the active tenant.
    """
    reader = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    writer = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    product = Product.objects.create(name="Widget", unit_price=500)
    payment = pay(writer, [(issued_invoice(writer, customer), 1000)])
    rate = TaxRate.objects.create(name="Austin", rate_bp=825)
    proposal = propose("1-1", [], [])
    assert list(list_customers(reader)) == [customer]
    assert list(list_products(reader)) == [product]
    assert list(list_payments(reader)) == [payment]
    assert list(list_tax_rates(reader)) == [rate]
    assert list(list_reconciliation_runs(reader)) == [proposal.run]
    assert list(list_match_proposals(reader)) == [proposal]
