"""Billing services: the write path for customers and issuer details.

Views, the admin and later the API call these functions, so audit logging and business rules
have a single place to live.
"""

from django.core.exceptions import PermissionDenied

from billing.models import Customer
from tenancy.context import require_current_tenant_id
from tenancy.models import Tenant
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

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        customer: Customer to persist.

    Returns:
        The saved customer.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
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
