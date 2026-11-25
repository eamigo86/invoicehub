"""Billing services: the write path for customers.

Views, the admin and later the API call these functions, so audit logging and business rules
have a single place to live.
"""

from billing.models import Customer
from tenancy.permissions import Permission, Principal, require_permission


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
