"""Billing services: the write path for customers and issuer details.

Views, the admin and later the API call these functions, so audit logging and business rules
have a single place to live.

Cross-tenant references are refused by validation: full_clean checks a foreign key through the
related model's base manager, which Row-Level Security limits to the current tenant, so a row of
another tenant raises ValidationError.
"""
# ponytail: writes that bypass the services are not checked; composite (tenant_id, id) foreign
# keys would enforce it in the database.

from collections.abc import Iterable

from django.core.exceptions import PermissionDenied
from django.db import transaction

from billing.models import Customer, Product, ProductTaxRate, TaxRate
from tenancy.context import require_current_tenant_id
from tenancy.models import Tenant, TenantModel
from tenancy.permissions import Permission, Principal, require_permission

ISSUER_FIELDS = [
    "legal_name",
    "ein",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    "default_currency",
    "timezone",
]
"""Tenant columns that update_issuer may write."""


def update_issuer(principal: Principal | None, tenant: Tenant) -> Tenant:
    """Validate and save the issuer details of the current tenant.

    Only the issuer fields are written, so no other tenant column can change through here.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        tenant: Tenant carrying the new issuer values, which must be the current tenant.

    Returns:
        The saved tenant.

    Raises:
        PermissionDenied: When the tenant is not the current tenant.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    if tenant.pk != require_current_tenant_id():
        raise PermissionDenied("tenant is not the current tenant")
    tenant.full_clean(exclude=["name"])
    tenant.save(update_fields=ISSUER_FIELDS)
    return tenant


def save_customer(principal: Principal | None, customer: Customer) -> Customer:
    """Create or update a customer for the current tenant.

    The customer is validated first, so ValidationError is raised for invalid data.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        customer: Customer to persist.

    Returns:
        The saved customer.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    customer.full_clean(exclude=["tenant"])
    customer.save()
    return customer


def delete_customer(principal: Principal | None, customer: Customer) -> None:
    """Delete a customer.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        customer: Customer to remove.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    customer.delete()


def _clean(row: TenantModel) -> None:
    """Validate a row of the current tenant, uniqueness within the tenant included.

    Args:
        row: Row to validate, which receives the current tenant when it has none.
    """
    if row.tenant_id is None:
        row.tenant_id = require_current_tenant_id()
    row.full_clean()


def save_tax_rate(principal: Principal | None, tax_rate: TaxRate) -> TaxRate:
    """Create or update a tax rate of the current tenant.

    The tax rate is validated first, so ValidationError is raised for invalid data.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        tax_rate: Tax rate to persist.

    Returns:
        The saved tax rate.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    _clean(tax_rate)
    tax_rate.save()
    return tax_rate


def delete_tax_rate(principal: Principal | None, tax_rate: TaxRate) -> None:
    """Delete a tax rate.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        tax_rate: Tax rate to remove.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    tax_rate.delete()


@transaction.atomic
def save_product(
    principal: Principal | None,
    product: Product,
    default_taxes: Iterable[TaxRate] | None = None,
) -> Product:
    """Create or update a product, optionally replacing its default taxes.

    Everything is validated, so ValidationError is raised for invalid data, including a tax
    rate of another tenant, and then nothing is stored.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        product: Product to persist.
        default_taxes: Tax rates the product should link to, replacing the current links, or
            None to leave them alone.

    Returns:
        The saved product.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    _clean(product)
    product.save()
    if default_taxes is not None:
        ProductTaxRate.objects.filter(product=product).delete()
        for rate in dict.fromkeys(default_taxes):
            link = ProductTaxRate(product=product, tax_rate=rate)
            _clean(link)
            link.save()
    return product


def delete_product(principal: Principal | None, product: Product) -> None:
    """Delete a product together with its default tax links.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        product: Product to remove.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    product.delete()
