"""Billing services: the write path for customers.

Views, the admin and later the API call these functions, so audit logging and business rules
have a single place to live.
"""

from billing.models import Customer


def save_customer(customer: Customer) -> Customer:
    """Create or update a customer for the current tenant.

    Args:
        customer: Customer to persist.

    Returns:
        The saved customer.
    """
    customer.save()
    return customer


def delete_customer(customer: Customer) -> None:
    """Delete a customer.

    Args:
        customer: Customer to remove.
    """
    customer.delete()
